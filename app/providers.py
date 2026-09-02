"""Open-data footprint and LiDAR discovery providers."""

from __future__ import annotations

import json
import hashlib
import csv
import gzip
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
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
    provider: str = "Overture Maps Buildings"
    license: str = "ODbL-1.0"
    attribution: str = "Overture Maps Foundation and source contributors"
    consensus_status: str = "SINGLE_SOURCE"
    consensus_records: tuple[dict[str, Any], ...] = ()
    lineage_group: str = "OPEN_MAP_FAMILY"


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


def _select_footprint_feature(
    features: list[dict[str, Any]],
    longitude: float,
    latitude: float,
    settings: Settings,
    *,
    provider: str,
    release: str,
    license_name: str,
    attribution: str,
) -> FootprintResult:
    """Select one valid, unambiguous building polygon from a provider response."""

    epsg = utm_epsg(longitude, latitude)
    point = Point(longitude, latitude)
    point_projected = transform_geometry(point, "EPSG:4326", f"EPSG:{epsg}")
    candidates: list[tuple[float, float, dict[str, Any], BaseGeometry]] = []
    for feature in features:
        try:
            geometry = shape(feature.get("geometry"))
        except Exception:
            continue
        if geometry.geom_type == "MultiPolygon":
            containing = [part for part in geometry.geoms if part.covers(point)]
            if len(containing) == 1:
                geometry = containing[0]
            elif len(geometry.geoms) == 1:
                geometry = geometry.geoms[0]
            else:
                continue
        if geometry.geom_type != "Polygon" or geometry.is_empty or not geometry.is_valid:
            continue
        projected = transform_geometry(geometry, "EPSG:4326", f"EPSG:{epsg}")
        candidates.append(
            (float(projected.distance(point_projected)), float(projected.area), feature, geometry)
        )

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    if not candidates or candidates[0][0] > settings.footprint_max_distance_meters:
        raise NoCoverageError(
            "BUILDING_FOOTPRINT_NOT_FOUND",
            f"No {provider} building footprint matched the geocoded property point.",
        )
    if len(candidates) > 1:
        first, second = candidates[0], candidates[1]
        if (
            second[0] <= settings.footprint_max_distance_meters
            and abs(second[0] - first[0]) <= settings.footprint_ambiguity_meters
        ):
            raise UnreliableGeometryError(
                "BUILDING_FOOTPRINT_AMBIGUOUS",
                f"Multiple {provider} building footprints are too close to the geocoded property point.",
            )

    distance, _, feature, geometry = candidates[0]
    properties = feature.get("properties") or {}
    source_id = str(feature.get("id") or properties.get("id") or "").strip()
    if not source_id:
        digest = hashlib.sha256(
            json.dumps(mapping(geometry), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        source_id = f"geometry-{digest}"
    records = properties.get("sources") if isinstance(properties.get("sources"), list) else []
    return FootprintResult(
        geometry,
        source_id,
        release,
        distance,
        records,
        provider,
        license_name,
        attribution,
    )


def _footprint_comparison(first: BaseGeometry, second: BaseGeometry) -> dict[str, float]:
    longitude = float(first.centroid.x)
    latitude = float(first.centroid.y)
    epsg = utm_epsg(longitude, latitude)
    projected_first = transform_geometry(first, "EPSG:4326", f"EPSG:{epsg}")
    projected_second = transform_geometry(second, "EPSG:4326", f"EPSG:{epsg}")
    first_area = float(projected_first.area)
    second_area = float(projected_second.area)
    union_area = projected_first.union(projected_second).area
    iou = 0.0 if union_area <= 0 else float(projected_first.intersection(projected_second).area / union_area)
    area_difference = abs(first_area - second_area) / max(first_area, second_area, 0.01) * 100
    return {
        "intersectionOverUnion": iou,
        "centroidSeparationMeters": float(projected_first.centroid.distance(projected_second.centroid)),
        "areaDifferencePercent": area_difference,
        "boundaryHausdorffDistanceMeters": float(
            projected_first.boundary.hausdorff_distance(projected_second.boundary)
        ),
    }


def _footprint_iou(first: BaseGeometry, second: BaseGeometry) -> float:
    """Backward-compatible scalar used by the imagery validator and tests."""

    return _footprint_comparison(first, second)["intersectionOverUnion"]


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

    result = _select_footprint_feature(
        list(payload.get("features") or []),
        longitude,
        latitude,
        settings,
        provider="Overture Maps Buildings",
        release=str(release),
        license_name="ODbL-1.0",
        attribution="Overture Maps Foundation and source contributors",
    )
    if result.overture_id.startswith("geometry-"):
        raise UnreliableGeometryError(
            "BUILDING_FOOTPRINT_ID_MISSING", "The selected Overture footprint has no stable identifier."
        )
    return result


def _bing_quadkey(longitude: float, latitude: float, zoom: int) -> str:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    x = (longitude + 180.0) / 360.0
    sin_latitude = math.sin(math.radians(latitude))
    y = 0.5 - math.log((1 + sin_latitude) / (1 - sin_latitude)) / (4 * math.pi)
    map_size = 1 << zoom
    tile_x = min(map_size - 1, max(0, int(x * map_size)))
    tile_y = min(map_size - 1, max(0, int(y * map_size)))
    digits: list[str] = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        digit = (1 if tile_x & mask else 0) + (2 if tile_y & mask else 0)
        digits.append(str(digit))
    return "".join(digits)


def _microsoft_catalog_row(longitude: float, latitude: float, settings: Settings) -> dict[str, str]:
    catalog = settings.work_root / f"microsoft-bfp-{settings.microsoft_bfp_release}-catalog.csv"
    if not catalog.exists() or time.time() - catalog.stat().st_mtime > settings.catalog_cache_seconds:
        temporary = catalog.with_suffix(".download")
        temporary.unlink(missing_ok=True)
        try:
            _download_file(
                settings.microsoft_bfp_catalog_url,
                temporary,
                timeout=settings.catalog_download_timeout_seconds,
                maximum_bytes=min(settings.catalog_maximum_bytes, 25_000_000),
            )
            os.replace(temporary, catalog)
        finally:
            temporary.unlink(missing_ok=True)
    quadkey = _bing_quadkey(longitude, latitude, settings.microsoft_bfp_zoom)
    try:
        with catalog.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw_row in csv.DictReader(handle):
                row = {str(key).strip().lower(): str(value or "").strip() for key, value in raw_row.items()}
                if row.get("location", "").lower() in {"unitedstates", "united states"} and row.get(
                    "quadkey"
                ) == quadkey:
                    return row
    except (OSError, csv.Error) as error:
        catalog.unlink(missing_ok=True)
        raise TransientProviderError(
            "MICROSOFT_FOOTPRINT_CATALOG_INVALID",
            "The Microsoft building-footprint catalog is unavailable or invalid.",
        ) from error
    raise NoCoverageError(
        "MICROSOFT_FOOTPRINT_TILE_NOT_FOUND",
        "Microsoft does not publish a building-footprint tile for this location.",
    )


def fetch_microsoft_footprint(
    longitude: float, latitude: float, workspace: Path, settings: Settings
) -> FootprintResult:
    """Select a building from Microsoft's pinned GlobalML footprint release."""

    if not settings.microsoft_bfp_enabled:
        raise NoCoverageError(
            "MICROSOFT_FOOTPRINT_DISABLED", "The Microsoft building-footprint fallback is disabled."
        )
    row = _microsoft_catalog_row(longitude, latitude, settings)
    url = row.get("url") or row.get("downloadurl") or row.get("download_url") or ""
    if not url.startswith("https://"):
        raise TransientProviderError(
            "MICROSOFT_FOOTPRINT_CATALOG_INVALID",
            "The Microsoft building-footprint catalog contains an invalid tile URL.",
        )
    quadkey = row.get("quadkey") or _bing_quadkey(longitude, latitude, settings.microsoft_bfp_zoom)
    tile = settings.work_root / f"microsoft-bfp-{settings.microsoft_bfp_release}-{quadkey}.csv.gz"
    if not tile.exists():
        temporary = tile.with_suffix(".download")
        temporary.unlink(missing_ok=True)
        try:
            _download_file(
                url,
                temporary,
                timeout=settings.catalog_download_timeout_seconds,
                maximum_bytes=settings.microsoft_bfp_maximum_tile_bytes,
            )
            os.replace(temporary, tile)
        finally:
            temporary.unlink(missing_ok=True)

    west, south, east, north = _bbox(longitude, latitude, settings.footprint_search_radius_meters)
    search = box(west, south, east, north)
    features: list[dict[str, Any]] = []
    try:
        with gzip.open(tile, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                batch = raw.get("features") if raw.get("type") == "FeatureCollection" else [raw]
                for feature in batch or []:
                    if not isinstance(feature, dict) or not feature.get("geometry"):
                        continue
                    try:
                        geometry = shape(feature["geometry"])
                    except Exception:
                        continue
                    if not geometry.is_empty and geometry.intersects(search):
                        candidate = dict(feature)
                        candidate.setdefault("id", f"{quadkey}:{line_number}")
                        features.append(candidate)
    except (OSError, EOFError, json.JSONDecodeError) as error:
        tile.unlink(missing_ok=True)
        raise TransientProviderError(
            "MICROSOFT_FOOTPRINT_TILE_INVALID",
            "The Microsoft building-footprint tile is unavailable or invalid.",
        ) from error
    return _select_footprint_feature(
        features,
        longitude,
        latitude,
        settings,
        provider="Microsoft GlobalML Building Footprints",
        release=settings.microsoft_bfp_release,
        license_name="CDLA-Permissive-2.0",
        attribution="Microsoft GlobalML Building Footprints",
    )


def fetch_county_footprint(
    longitude: float, latitude: float, workspace: Path, settings: Settings
) -> FootprintResult:
    """Reuse an explicitly authorized county building layer when it covers the point."""

    del workspace
    county = resolve_service_county_point(longitude, latitude, settings)
    source = {
        "Lee": {
            "url": settings.lee_county_footprint_url,
            "release": "2026-03-22",
            "license": "LEE-COUNTY-PUBLIC-GIS",
            "attribution": "Lee County Property Appraiser and Lee County GIS",
        }
    }.get(county)
    if source is None:
        raise NoCoverageError(
            "COUNTY_FOOTPRINT_UNAVAILABLE",
            "No explicitly authorized county building-footprint layer covers this property.",
        )
    west, south, east, north = _bbox(longitude, latitude, settings.footprint_search_radius_meters)
    parameters = urllib.parse.urlencode(
        {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        }
    )
    request = urllib.request.Request(
        f"{source['url']}?{parameters}", headers={"User-Agent": "CapeRoofGeometry/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.provider_timeout_seconds) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise TransientProviderError(
            "COUNTY_FOOTPRINT_UNAVAILABLE", "The county building-footprint service is unavailable."
        ) from error
    result = _select_footprint_feature(
        list(payload.get("features") or []),
        longitude,
        latitude,
        settings,
        provider=f"{county} County Building Footprints",
        release=str(source["release"]),
        license_name=str(source["license"]),
        attribution=str(source["attribution"]),
    )
    return replace(result, lineage_group="COUNTY_AUTHORITATIVE")


def fetch_osm_footprint(
    longitude: float, latitude: float, workspace: Path, settings: Settings
) -> FootprintResult:
    """Query a small Overpass window as the final open footprint fallback."""

    del workspace
    if not settings.osm_footprint_enabled:
        raise NoCoverageError("OSM_FOOTPRINT_DISABLED", "The OpenStreetMap footprint fallback is disabled.")
    west, south, east, north = _bbox(longitude, latitude, settings.footprint_search_radius_meters)
    query = (
        f"[out:json][timeout:{max(10, settings.provider_timeout_seconds - 5)}];"
        f"way[\"building\"]({south},{west},{north},{east});out geom meta;"
    )
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
    cache = settings.work_root / f"osm-footprints-{digest}.json"
    payload: dict[str, Any]
    if cache.exists() and time.time() - cache.stat().st_mtime <= settings.osm_cache_seconds:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache.unlink(missing_ok=True)
            payload = {}
    else:
        payload = {}
    if not payload:
        request = urllib.request.Request(
            settings.osm_overpass_url,
            data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
            headers={
                "User-Agent": "CapeRoofGeometry/1.0 (https://github.com/AlexRoofing1949/cape-roof-geometry)",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.provider_timeout_seconds) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise TransientProviderError(
                "OSM_OVERPASS_UNAVAILABLE", "The OpenStreetMap footprint service is unavailable."
            ) from error
        temporary = cache.with_suffix(".download")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, cache)
        except OSError:
            temporary.unlink(missing_ok=True)
    features: list[dict[str, Any]] = []
    for element in payload.get("elements") or []:
        vertices = element.get("geometry") if isinstance(element, dict) else None
        if not isinstance(vertices, list) or len(vertices) < 4:
            continue
        coordinates = [
            [float(vertex["lon"]), float(vertex["lat"])]
            for vertex in vertices
            if isinstance(vertex, dict) and "lon" in vertex and "lat" in vertex
        ]
        if len(coordinates) < 4:
            continue
        if coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0])
        features.append(
            {
                "type": "Feature",
                "id": f"way/{element.get('id')}",
                "properties": {
                    "version": element.get("version"),
                    "timestamp": element.get("timestamp"),
                    "sources": [{"dataset": "OpenStreetMap", "recordId": f"way/{element.get('id')}"}],
                },
                "geometry": {"type": "Polygon", "coordinates": [coordinates]},
            }
        )
    return _select_footprint_feature(
        features,
        longitude,
        latitude,
        settings,
        provider="OpenStreetMap Buildings",
        release="live-overpass",
        license_name="ODbL-1.0",
        attribution="OpenStreetMap contributors",
    )


def fetch_best_footprint(
    longitude: float, latitude: float, workspace: Path, settings: Settings
) -> FootprintResult:
    """Run the authorized footprint cascade and fail closed on material disagreement."""

    providers = (
        fetch_overture_footprint,
        fetch_microsoft_footprint,
        fetch_county_footprint,
        fetch_osm_footprint,
    )
    primary: FootprintResult | None = None
    selected_index = -1
    audit: list[dict[str, Any]] = []
    for index, provider in enumerate(providers):
        provider_name = getattr(provider, "__name__", provider.__class__.__name__)
        try:
            primary = provider(longitude, latitude, workspace, settings)
            selected_index = index
            audit.append(
                {
                    "provider": primary.provider,
                    "lineageGroup": primary.lineage_group,
                    "decision": "SELECTED",
                }
            )
            break
        except UnreliableGeometryError:
            raise
        except (NoCoverageError, TransientProviderError) as error:
            audit.append(
                {
                    "provider": provider_name,
                    "decision": "UNAVAILABLE",
                    "errorCode": error.code,
                }
            )
    if primary is None:
        raise UnreliableGeometryError(
            "BUILDING_FOOTPRINT_NOT_FOUND",
            "No authorized open building-footprint provider produced a reliable property polygon.",
            details={"providerAttempts": audit},
        )

    # Corroborate with the first available independent source. Microsoft is not
    # downloaded merely to check a successful Overture result; its large tile is
    # reserved for an actual fallback path.
    corroborators = list(providers[selected_index + 1 :])
    if selected_index == 0:
        corroborators = [fetch_county_footprint, fetch_osm_footprint]
    correlated_support = False
    for provider in corroborators:
        provider_name = getattr(provider, "__name__", provider.__class__.__name__)
        try:
            secondary = provider(longitude, latitude, workspace, settings)
        except (NoCoverageError, TransientProviderError, UnreliableGeometryError) as error:
            audit.append(
                {
                    "provider": provider_name,
                    "decision": "CORROBORATION_UNAVAILABLE",
                    "errorCode": error.code,
                }
            )
            continue
        comparison = _footprint_comparison(primary.geometry_wgs84, secondary.geometry_wgs84)
        independent = primary.lineage_group != secondary.lineage_group
        record = {
            "provider": secondary.provider,
            "id": secondary.overture_id,
            "release": secondary.overture_release,
            "lineageGroup": secondary.lineage_group,
            "independent": independent,
            "intersectionOverUnion": round(comparison["intersectionOverUnion"], 4),
            "centroidSeparationMeters": round(comparison["centroidSeparationMeters"], 3),
            "areaDifferencePercent": round(comparison["areaDifferencePercent"], 3),
            "boundaryHausdorffDistanceMeters": round(
                comparison["boundaryHausdorffDistanceMeters"], 3
            ),
        }
        if not independent:
            correlated_passed = (
                comparison["intersectionOverUnion"] >= settings.footprint_correlated_min_iou
                and comparison["areaDifferencePercent"] <= settings.footprint_review_area_difference_percent
            )
            audit.append(
                {
                    **record,
                    "decision": "CORRELATED_SUPPORT_ONLY" if correlated_passed else "CORRELATED_CONFLICT",
                }
            )
            if not correlated_passed:
                raise UnreliableGeometryError(
                    "FOOTPRINT_PROVIDER_CONFLICT",
                    "Related open-map building sources materially disagree for this property.",
                    details={
                        "primaryProvider": primary.provider,
                        "secondaryProvider": secondary.provider,
                        **{key: round(value, 4) for key, value in comparison.items()},
                    },
                )
            correlated_support = True
            continue

        independent_passed = (
            comparison["intersectionOverUnion"] >= settings.footprint_consensus_min_iou
            and comparison["centroidSeparationMeters"]
            <= settings.footprint_maximum_centroid_separation_meters
            and comparison["areaDifferencePercent"]
            <= settings.footprint_maximum_area_difference_percent
        )
        audit.append({**record, "decision": "CORROBORATED" if independent_passed else "CONFLICT"})
        if not independent_passed:
            raise UnreliableGeometryError(
                "FOOTPRINT_PROVIDER_CONFLICT",
                "Independent building-footprint providers materially disagree for this property.",
                details={
                    "primaryProvider": primary.provider,
                    "secondaryProvider": secondary.provider,
                    **{key: round(value, 4) for key, value in comparison.items()},
                    "minimumIntersectionOverUnion": settings.footprint_consensus_min_iou,
                    "maximumCentroidSeparationMeters": settings.footprint_maximum_centroid_separation_meters,
                    "maximumAreaDifferencePercent": settings.footprint_maximum_area_difference_percent,
                },
            )
        return replace(primary, consensus_status="CORROBORATED", consensus_records=tuple(audit))
    return replace(
        primary,
        consensus_status="CORRELATED_SUPPORT_ONLY" if correlated_support else "SINGLE_SOURCE",
        consensus_records=tuple(audit),
    )


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
                        "properties": {
                            "request_id": request_id,
                            "source_id": footprint.overture_id,
                            "source_provider": footprint.provider,
                        },
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
        # TIGERweb currently returns values such as "Lee County", while the
        # pinned LiDAR registry intentionally uses canonical county names.
        name = re.sub(r"\s+County$", "", raw_name, flags=re.IGNORECASE).strip()
        name = "DeSoto" if name.replace(" ", "").lower() == "desoto" else name
        if name not in SERVICE_COUNTIES:
            continue
        ratio = _coverage_ratio(geometry, footprint_wgs84)
        if ratio > 0:
            matches.append((ratio, name))
    matches.sort(reverse=True)
    if not matches or matches[0][0] < 0.98:
        raise NoCoverageError("OUTSIDE_SERVICE_AREA", "The selected building is outside the eight-county service area.")
    return matches[0][1]


def resolve_service_county_point(longitude: float, latitude: float, settings: Settings) -> str:
    """Resolve the service county before a building footprint is available."""

    payload = _cached_json_url(settings.county_boundaries_url, "florida-counties", settings)
    point = Point(longitude, latitude)
    matches: list[str] = []
    for feature in payload.get("features") or []:
        try:
            geometry = shape(feature.get("geometry"))
        except Exception:
            continue
        properties = feature.get("properties") or {}
        raw_name = str(properties.get("NAME") or properties.get("name") or "").strip()
        name = re.sub(r"\s+County$", "", raw_name, flags=re.IGNORECASE).strip()
        name = "DeSoto" if name.replace(" ", "").lower() == "desoto" else name
        if name in SERVICE_COUNTIES and geometry.covers(point):
            matches.append(name)
    if len(matches) != 1:
        raise NoCoverageError(
            "OUTSIDE_SERVICE_AREA", "The geocoded property point is outside the eight-county service area."
        )
    return matches[0]


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
            # Per-tile GPS dates are only available after the property crop is
            # downloaded.  Use the registered acquisition end date for the
            # initial ordering so a slightly larger coverage polygon cannot
            # cause an older source to be attempted before a newer one.
            item.acquired_end,
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
