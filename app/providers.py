"""Open-data footprint and LiDAR discovery providers."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ijson
from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from .config import Settings
from .errors import NoCoverageError, TransientProviderError, UnreliableGeometryError
from .source_registry import LidarSource, RegistryBundle, SERVICE_COUNTIES


_CATALOG_LOCK = threading.Lock()
_OVERTURE_STAC_CATALOG_URL = "https://stac.overturemaps.org/catalog.json"


@dataclass(frozen=True)
class FootprintResult:
    geometry_wgs84: BaseGeometry
    overture_id: str
    overture_release: str
    distance_meters: float
    source_records: list[dict[str, Any]]


@dataclass(frozen=True)
class LidarResource:
    source_id: str
    dataset_name: str
    provider: str
    ept_url: str
    acquired_start: str
    acquired_end: str
    tile_acquisition_date: str
    age_years: int
    point_count: int
    coverage_ratio: float
    county: str
    allowed_classes: tuple[int, ...]
    roof_classes: tuple[int, ...]
    minimum_density_ppsm: float
    license: str
    metadata_url: str
    selection_reason: str


def utm_epsg(longitude: float, latitude: float) -> int:
    zone = max(1, min(60, int(math.floor((longitude + 180) / 6)) + 1))
    return (32600 if latitude >= 0 else 32700) + zone


def transform_geometry(geometry: BaseGeometry, source: str, target: str) -> BaseGeometry:
    transformer = Transformer.from_crs(source, target, always_xy=True)
    return transform(transformer.transform, geometry)


def buffered_footprint_wgs84(
    footprint: BaseGeometry, longitude: float, latitude: float, buffer_meters: float
) -> BaseGeometry:
    epsg = utm_epsg(longitude, latitude)
    projected = transform_geometry(footprint, "EPSG:4326", f"EPSG:{epsg}")
    return transform_geometry(projected.buffer(buffer_meters), f"EPSG:{epsg}", "EPSG:4326")


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise TransientProviderError("OPEN_DATA_COMMAND_TIMEOUT", "An open-data provider command timed out.") from error
    except subprocess.CalledProcessError as error:
        safe_tail = (error.stderr or error.stdout or "")[-500:].replace("\n", " ")
        raise TransientProviderError(
            "OPEN_DATA_COMMAND_FAILED", f"An open-data provider command failed: {safe_tail}"
        ) from error
    except OSError as error:
        raise TransientProviderError(
            "OPEN_DATA_COMMAND_UNAVAILABLE", "An open-data provider command is unavailable."
        ) from error


def _bbox(longitude: float, latitude: float, radius_meters: float) -> tuple[float, float, float, float]:
    latitude_delta = radius_meters / 111_320
    longitude_delta = radius_meters / max(1, 111_320 * math.cos(math.radians(latitude)))
    return (
        longitude - longitude_delta,
        latitude - latitude_delta,
        longitude + longitude_delta,
        latitude + latitude_delta,
    )


def _current_overture_release(settings: Settings) -> str:
    request = urllib.request.Request(
        _OVERTURE_STAC_CATALOG_URL, headers={"User-Agent": "CapeRoofGeometry/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.provider_timeout_seconds) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise TransientProviderError(
            "OVERTURE_CATALOG_UNAVAILABLE", "The Overture release catalog is temporarily unavailable."
        ) from error
    latest = str(payload.get("latest") or "").strip() if isinstance(payload, dict) else ""
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}\.\d+", latest):
        raise TransientProviderError(
            "OVERTURE_CATALOG_INVALID", "The Overture release catalog did not identify a valid latest release."
        )
    return latest


def _require_pinned_overture_release(settings: Settings) -> str:
    latest = _current_overture_release(settings)
    if latest != settings.overture_release:
        raise UnreliableGeometryError(
            "OVERTURE_RELEASE_MISMATCH",
            "The configured Overture release is not the current published release.",
            details={"configuredRelease": settings.overture_release, "currentRelease": latest},
        )
    return latest


def fetch_overture_footprint(
    longitude: float, latitude: float, workspace: Path, settings: Settings
) -> FootprintResult:
    """Download only nearby Overture buildings and select one unambiguous polygon."""

    release = _require_pinned_overture_release(settings)
    west, south, east, north = _bbox(longitude, latitude, settings.footprint_search_radius_meters)
    output_path = workspace / "overture-buildings.geojson"
    _run(
        [
            "overturemaps",
            "download",
            f"--bbox={west},{south},{east},{north}",
            "-f",
            "geojson",
            "--type=building",
            "--output",
            str(output_path),
        ],
        timeout=settings.provider_timeout_seconds,
    )
    if _require_pinned_overture_release(settings) != release:
        raise UnreliableGeometryError(
            "OVERTURE_RELEASE_CHANGED_DURING_DOWNLOAD",
            "The Overture release changed while the building footprint was being downloaded.",
        )
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TransientProviderError("OVERTURE_RESPONSE_INVALID", "Overture returned an invalid building response.") from error

    epsg = utm_epsg(longitude, latitude)
    point_projected = transform_geometry(Point(longitude, latitude), "EPSG:4326", f"EPSG:{epsg}")
    candidates: list[tuple[float, float, dict[str, Any], BaseGeometry]] = []
    for feature in payload.get("features", []):
        try:
            geometry = shape(feature.get("geometry"))
        except Exception:
            continue
        if geometry.geom_type == "MultiPolygon":
            containing = [part for part in geometry.geoms if part.covers(Point(longitude, latitude))]
            if len(containing) == 1:
                geometry = containing[0]
            elif len(geometry.geoms) == 1:
                geometry = geometry.geoms[0]
            else:
                continue
        if geometry.geom_type != "Polygon" or geometry.is_empty or not geometry.is_valid:
            continue
        projected = transform_geometry(geometry, "EPSG:4326", f"EPSG:{epsg}")
        distance = float(projected.distance(point_projected))
        candidates.append((distance, float(projected.area), feature, geometry))

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    if not candidates or candidates[0][0] > settings.footprint_max_distance_meters:
        raise NoCoverageError("BUILDING_FOOTPRINT_NOT_FOUND", "No open building footprint matched the geocoded property point.")
    if len(candidates) > 1:
        first, second = candidates[0], candidates[1]
        if second[0] <= settings.footprint_max_distance_meters and abs(second[0] - first[0]) <= settings.footprint_ambiguity_meters:
            raise UnreliableGeometryError(
                "BUILDING_FOOTPRINT_AMBIGUOUS",
                "Multiple open building footprints are too close to the geocoded property point.",
            )

    distance, _, feature, geometry = candidates[0]
    properties = feature.get("properties") or {}
    overture_id = str(feature.get("id") or properties.get("id") or "").strip()
    if not overture_id:
        raise UnreliableGeometryError("BUILDING_FOOTPRINT_ID_MISSING", "The selected Overture footprint has no stable identifier.")
    source_records = properties.get("sources") if isinstance(properties.get("sources"), list) else []
    return FootprintResult(geometry, overture_id, str(release), distance, source_records)


def write_footprint_inputs(
    footprint: FootprintResult,
    request_id: str,
    workspace: Path,
    longitude: float,
    latitude: float,
    settings: Settings,
) -> tuple[Path, str, int, BaseGeometry]:
    """Create Roofer's projected GeoPackage and the WGS84 LiDAR crop polygon."""

    epsg = utm_epsg(longitude, latitude)
    projected = transform_geometry(footprint.geometry_wgs84, "EPSG:4326", f"EPSG:{epsg}")
    buffered_projected = projected.buffer(settings.lidar_buffer_meters)
    buffered_wgs84 = transform_geometry(buffered_projected, f"EPSG:{epsg}", "EPSG:4326")

    source_geojson = workspace / "selected-footprint.geojson"
    source_geojson.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": request_id,
                        "properties": {"request_id": request_id, "overture_id": footprint.overture_id},
                        "geometry": mapping(footprint.geometry_wgs84),
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    gpkg = workspace / "footprint.gpkg"
    _run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            str(gpkg),
            str(source_geojson),
            "-s_srs",
            "EPSG:4326",
            "-t_srs",
            f"EPSG:{epsg}",
            "-nln",
            "footprints",
        ],
        timeout=settings.command_timeout_seconds,
    )
    return gpkg, buffered_wgs84.wkt, epsg, projected


def _download_file(url: str, destination: Path, *, timeout: int, maximum_bytes: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "CapeRoofGeometry/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                content_length = int(response.headers.get("Content-Length", "0") or 0)
            except ValueError:
                content_length = 0
            if content_length > maximum_bytes:
                raise TransientProviderError("PROVIDER_RESPONSE_TOO_LARGE", "An open-data provider response exceeded its size limit.")
            written = 0
            with destination.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > maximum_bytes:
                        raise TransientProviderError(
                            "PROVIDER_RESPONSE_TOO_LARGE",
                            "An open-data provider response exceeded its size limit.",
                        )
                    handle.write(chunk)
    except urllib.error.HTTPError as error:
        if error.code >= 500 or error.code in {408, 429}:
            raise TransientProviderError("PROVIDER_HTTP_ERROR", "An open-data provider is temporarily unavailable.") from error
        raise NoCoverageError("PROVIDER_RESOURCE_NOT_FOUND", "An open-data provider resource was not found.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TransientProviderError("PROVIDER_NETWORK_ERROR", "An open-data provider is temporarily unavailable.") from error


def _catalog_path(settings: Settings) -> Path:
    cache = settings.work_root / "usgs-3dep-resources.geojson"
    if cache.exists() and time.time() - cache.stat().st_mtime <= settings.catalog_cache_seconds:
        return cache
    with _CATALOG_LOCK:
        if cache.exists() and time.time() - cache.stat().st_mtime <= settings.catalog_cache_seconds:
            return cache
        temporary = cache.with_suffix(".download")
        temporary.unlink(missing_ok=True)
        try:
            _download_file(
                settings.usgs_catalog_url,
                temporary,
                timeout=settings.catalog_download_timeout_seconds,
                maximum_bytes=settings.catalog_maximum_bytes,
            )
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
    return cache


def _catalog_features(settings: Settings):
    """Stream the large USGS boundary catalog without loading it into RAM."""

    path = _catalog_path(settings)
    try:
        with path.open("rb") as handle:
            yield from ijson.items(handle, "features.item")
    except (OSError, ijson.JSONError) as error:
        path.unlink(missing_ok=True)
        raise TransientProviderError(
            "PROVIDER_JSON_INVALID", "The USGS 3DEP catalog is unavailable or invalid."
        ) from error


def _coverage_ratio(coverage_wgs84: BaseGeometry, target_wgs84: BaseGeometry) -> float:
    if coverage_wgs84.is_empty or target_wgs84.is_empty:
        return 0.0
    longitude = float(target_wgs84.centroid.x)
    latitude = float(target_wgs84.centroid.y)
    epsg = utm_epsg(longitude, latitude)
    coverage = transform_geometry(coverage_wgs84, "EPSG:4326", f"EPSG:{epsg}")
    target = transform_geometry(target_wgs84, "EPSG:4326", f"EPSG:{epsg}")
    return max(0.0, min(1.0, float(coverage.intersection(target).area / max(target.area, 0.01))))


def _cached_json_url(url: str, prefix: str, settings: Settings) -> dict[str, Any]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    cache = settings.work_root / f"{prefix}-{digest}.json"
    if not cache.exists() or time.time() - cache.stat().st_mtime > settings.catalog_cache_seconds:
        temporary = cache.with_suffix(".download")
        temporary.unlink(missing_ok=True)
        try:
            _download_file(
                url,
                temporary,
                timeout=settings.catalog_download_timeout_seconds,
                maximum_bytes=min(settings.catalog_maximum_bytes, 25_000_000),
            )
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        cache.unlink(missing_ok=True)
        raise TransientProviderError("PROVIDER_JSON_INVALID", "An open-data metadata response is invalid.") from error
    if not isinstance(payload, dict):
        raise TransientProviderError("PROVIDER_JSON_INVALID", "An open-data metadata response is invalid.")
    return payload


def resolve_service_county(footprint_wgs84: BaseGeometry, settings: Settings) -> str:
    payload = _cached_json_url(settings.county_boundaries_url, "florida-counties", settings)
    matches: list[tuple[float, str]] = []
    for feature in payload.get("features") or []:
        try:
            geometry = shape(feature.get("geometry"))
        except Exception:
            continue
        properties = feature.get("properties") or {}
        raw_name = str(properties.get("NAME") or properties.get("name") or "").strip()
        name = "DeSoto" if raw_name.replace(" ", "").lower() == "desoto" else raw_name
        if name not in SERVICE_COUNTIES:
            continue
        ratio = _coverage_ratio(geometry, footprint_wgs84)
        if ratio > 0:
            matches.append((ratio, name))
    matches.sort(reverse=True)
    if not matches or matches[0][0] < 0.98:
        raise NoCoverageError("OUTSIDE_SERVICE_AREA", "The selected building is outside the eight-county service area.")
    return matches[0][1]


def _ept_coverage(endpoint: str, settings: Settings) -> tuple[BaseGeometry, int]:
    payload = _cached_json_url(endpoint, "ept", settings)
    bounds = payload.get("boundsConforming") or payload.get("bounds")
    if not isinstance(bounds, list) or len(bounds) < 6:
        raise TransientProviderError("EPT_METADATA_INVALID", "The EPT source does not report conforming bounds.")
    srs = payload.get("srs") or {}
    source_crs: str | None = None
    if isinstance(srs, dict):
        if srs.get("wkt"):
            source_crs = str(srs["wkt"])
        elif srs.get("horizontal"):
            authority = str(srs.get("authority") or "EPSG")
            source_crs = f"{authority}:{srs['horizontal']}"
    if not source_crs:
        raise TransientProviderError("EPT_CRS_MISSING", "The EPT source does not report a horizontal CRS.")
    try:
        coverage = box(float(bounds[0]), float(bounds[1]), float(bounds[3]), float(bounds[4]))
        coverage_wgs84 = transform_geometry(coverage, source_crs, "EPSG:4326")
        point_count = int(payload.get("points") or 0)
    except Exception as error:
        raise TransientProviderError("EPT_METADATA_INVALID", "The EPT source metadata is unusable.") from error
    return coverage_wgs84, point_count


def _resource_from_source(
    source: LidarSource,
    *,
    endpoint: str,
    coverage_ratio: float,
    county: str,
    point_count: int,
    tile_acquisition_date: str = "",
) -> LidarResource:
    return LidarResource(
        source_id=source.id,
        dataset_name=source.dataset_name,
        provider=source.provider,
        ept_url=endpoint,
        acquired_start=source.acquired_start.isoformat(),
        acquired_end=source.acquired_end.isoformat(),
        tile_acquisition_date=tile_acquisition_date,
        age_years=source.age_years,
        point_count=point_count,
        coverage_ratio=round(coverage_ratio, 6),
        county=county,
        allowed_classes=source.allowed_classes,
        roof_classes=source.roof_classes,
        minimum_density_ppsm=source.minimum_density_ppsm,
        license=source.license,
        metadata_url=source.metadata_url,
        selection_reason="newest enabled registered source with complete buffered-footprint coverage",
    )


def select_regional_lidar(
    footprint: FootprintResult,
    longitude: float,
    latitude: float,
    settings: Settings,
    registries: RegistryBundle,
) -> tuple[str, list[LidarResource], list[dict[str, Any]]]:
    """Resolve the county and return ordered, registered LiDAR candidates."""

    buffered = buffered_footprint_wgs84(
        footprint.geometry_wgs84, longitude, latitude, settings.lidar_buffer_meters
    )
    county = resolve_service_county(footprint.geometry_wgs84, settings)
    candidates: list[LidarResource] = []
    audit: list[dict[str, Any]] = []
    catalog_features: list[dict[str, Any]] | None = None

    for source in registries.lidar_sources:
        record: dict[str, Any] = {
            "sourceId": source.id,
            "enabled": source.enabled,
            "licenseVerified": source.license_verified,
            "countyEligible": county in source.counties,
            "acquiredEnd": source.acquired_end.isoformat(),
        }
        if not source.enabled or county not in source.counties:
            record["decision"] = "SKIPPED_DISABLED_OR_COUNTY"
            audit.append(record)
            continue
        if not source.license_verified:
            record["decision"] = "REJECTED_LICENSE_UNVERIFIED"
            audit.append(record)
            continue

        if source.access == "usgs_tnm":
            if catalog_features is None:
                catalog_features = list(_catalog_features(settings))
            matched = False
            for feature in catalog_features:
                properties = feature.get("properties") or {}
                name = str(properties.get("name") or "")
                if not any(re.search(pattern, name) for pattern in source.catalog_name_patterns):
                    continue
                url = str(properties.get("url") or "")
                if not re.fullmatch(
                    r"https://(?:s3-us-west-2\.amazonaws\.com/usgs-lidar-public|usgs-lidar-public\.s3\.amazonaws\.com)/[A-Za-z0-9_.-]+/ept\.json",
                    url,
                ):
                    continue
                try:
                    ratio = _coverage_ratio(shape(feature.get("geometry")), buffered)
                except Exception:
                    continue
                if ratio < settings.minimum_lidar_coverage_ratio:
                    continue
                candidates.append(
                    _resource_from_source(
                        source,
                        endpoint=url,
                        coverage_ratio=ratio,
                        county=county,
                        point_count=int(properties.get("count") or 0),
                    )
                )
                matched = True
            record["decision"] = "CANDIDATE" if matched else "NO_REGISTERED_CATALOG_COVERAGE"
            audit.append(record)
            continue

        if source.endpoint:
            coverage, point_count = _ept_coverage(source.endpoint, settings)
            ratio = _coverage_ratio(coverage, buffered)
            record["coverageRatio"] = round(ratio, 6)
            if ratio >= settings.minimum_lidar_coverage_ratio:
                candidates.append(
                    _resource_from_source(
                        source,
                        endpoint=source.endpoint,
                        coverage_ratio=ratio,
                        county=county,
                        point_count=point_count,
                    )
                )
                record["decision"] = "CANDIDATE"
            else:
                record["decision"] = "NO_COMPLETE_BUFFERED_COVERAGE"
            audit.append(record)

    candidates.sort(
        key=lambda item: (
            item.tile_acquisition_date,
            item.coverage_ratio,
            item.minimum_density_ppsm,
            next(source.priority for source in registries.lidar_sources if source.id == item.source_id),
        ),
        reverse=True,
    )
    if not candidates:
        raise NoCoverageError("NO_LIDAR_COVERAGE", "No enabled registered LiDAR source completely covers this building.")
    if candidates[0].age_years > settings.maximum_lidar_age_years:
        raise UnreliableGeometryError("LIDAR_DATA_TOO_OLD", "The newest registered LiDAR exceeds the maximum age threshold.")
    return county, candidates, audit
