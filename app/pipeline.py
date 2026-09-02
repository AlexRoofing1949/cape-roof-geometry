"""End-to-end open-source roof reconstruction pipeline."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cityjson_geometry import extract_roof_geometry, load_cityjson_feature
from .config import Settings
from .errors import TransientProviderError, UnreliableGeometryError
from .imagery_validation import validate_current_structure
from .models import GeometryRequest
from .providers import (
    fetch_overture_footprint,
    LidarResource,
    select_regional_lidar,
    write_footprint_inputs,
)
from .source_registry import load_registries


def _run(
    command: list[str], *, timeout: int, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise TransientProviderError("GEOMETRY_COMMAND_TIMEOUT", "The roof reconstruction command timed out.") from error
    except subprocess.CalledProcessError as error:
        safe_tail = (error.stderr or error.stdout or "")[-500:].replace("\n", " ")
        raise UnreliableGeometryError(
            "GEOMETRY_COMMAND_FAILED",
            f"The roof reconstruction command did not produce verified geometry: {safe_tail}",
        ) from error


def _pdal_crop(
    lidar: LidarResource,
    crop_wkt_wgs84: str,
    target_epsg: int,
    output_path: Path,
    workspace: Path,
    settings: Settings,
) -> dict[str, Any]:
    class_expression = " || ".join(
        f"Classification == {classification}" for classification in lidar.allowed_classes
    )
    metadata_path = workspace / "pdal-metadata.json"
    pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": lidar.ept_url,
                "polygon": f"{crop_wkt_wgs84}/EPSG:4326",
                "requests": 8,
            },
            {
                "type": "filters.expression",
                "expression": class_expression,
            },
            {
                "type": "filters.stats",
                "dimensions": "Classification,GpsTime",
                "count": "Classification",
            },
            {
                "type": "filters.reprojection",
                "out_srs": f"EPSG:{target_epsg}",
            },
            {
                "type": "writers.las",
                "filename": str(output_path),
                "a_srs": f"EPSG:{target_epsg}",
                "minor_version": 4,
                "dataformat_id": 6,
                "compression": "laszip",
                "scale_x": 0.01,
                "scale_y": 0.01,
                "scale_z": 0.01,
            },
        ]
    }
    pipeline_path = workspace / "pdal-pipeline.json"
    pipeline_path.write_text(json.dumps(pipeline, separators=(",", ":")), encoding="utf-8")
    _run(
        ["pdal", "pipeline", "--metadata", str(metadata_path), str(pipeline_path)],
        timeout=settings.command_timeout_seconds,
    )
    if not output_path.exists() or output_path.stat().st_size < 1024:
        raise UnreliableGeometryError(
            "LIDAR_CROP_EMPTY", "The selected regional source did not return usable classified points."
        )
    histogram = _classification_histogram(metadata_path)
    tile_acquisition_date = _exact_gps_acquisition_date(metadata_path, lidar)
    roof_return_count = sum(histogram.get(str(value), 0) for value in lidar.roof_classes)
    total_count = sum(histogram.values())
    if total_count <= 0 or roof_return_count <= 0:
        raise UnreliableGeometryError(
            "NO_USABLE_ROOF_RETURNS",
            "The selected source contains no registered usable roof-return classes in this crop.",
            details={"sourceId": lidar.source_id, "classHistogram": histogram},
        )
    return {
        "classHistogram": histogram,
        "filteredPointCount": total_count,
        "roofReturnCount": roof_return_count,
        "allowedClasses": list(lidar.allowed_classes),
        "roofClasses": list(lidar.roof_classes),
        "tileAcquisitionDate": tile_acquisition_date,
        "pipeline": pipeline,
    }


def _classification_histogram(metadata_path: Path) -> dict[str, int]:
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UnreliableGeometryError(
            "LIDAR_CLASSIFICATION_AUDIT_MISSING", "PDAL did not produce a readable classification audit."
        ) from error

    histogram: dict[str, int] = {}
    classification_seen = False
    classification_point_count = 0

    def visit(node: Any) -> None:
        nonlocal classification_seen, classification_point_count
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("dimension") or "")
            if name.lower() == "classification":
                classification_seen = True
                try:
                    classification_point_count += int(node.get("count") or 0)
                except (TypeError, ValueError):
                    pass
                counts = node.get("counts") or node.get("enumeration") or node.get("values")
                if isinstance(counts, list):
                    for item in counts:
                        # PDAL serializes repeated ``counts`` metadata children as
                        # strings in the form ``<dimension value>/<point count>``
                        # (for example ``6.000000/412``).  Some bindings instead
                        # expose objects, so accept both representations while
                        # still requiring numeric class IDs and integer counts.
                        if isinstance(item, str):
                            value, separator, count = item.partition("/")
                            if not separator:
                                continue
                            try:
                                class_id = str(int(float(value)))
                                histogram[class_id] = histogram.get(class_id, 0) + int(count)
                            except (TypeError, ValueError):
                                continue
                            continue
                        if not isinstance(item, dict):
                            continue
                        value = item.get("value", item.get("name"))
                        count = item.get("count")
                        try:
                            histogram[str(int(value))] = histogram.get(str(int(value)), 0) + int(count)
                        except (TypeError, ValueError):
                            continue
                elif isinstance(counts, dict):
                    for value, count in counts.items():
                        try:
                            histogram[str(int(value))] = histogram.get(str(int(value)), 0) + int(count)
                        except (TypeError, ValueError):
                            continue
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    if not histogram and classification_seen and classification_point_count == 0:
        return {}
    if not histogram:
        raise UnreliableGeometryError(
            "LIDAR_CLASSIFICATION_AUDIT_MISSING",
            "PDAL did not report an enumerated LAS classification histogram.",
        )
    return histogram


def _exact_gps_acquisition_date(metadata_path: Path, lidar: LidarResource) -> str:
    """Return one exact crop date only when GPS time and the registry agree."""

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    statistics: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if str(node.get("name") or "").lower() == "gpstime":
                statistics.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    if not statistics:
        return ""
    try:
        registered_start = date.fromisoformat(lidar.acquired_start)
        registered_end = date.fromisoformat(lidar.acquired_end)
    except ValueError:
        return ""
    gps_epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)
    matching_dates: set[date] = set()
    for statistic in statistics:
        for field in ("minimum", "maximum", "average", "mean", "median"):
            try:
                seconds = float(statistic[field])
            except (KeyError, TypeError, ValueError):
                continue
            for offset in (0.0, 1_000_000_000.0):
                try:
                    candidate = (gps_epoch + timedelta(seconds=seconds + offset)).date()
                except OverflowError:
                    continue
                if registered_start <= candidate <= registered_end:
                    matching_dates.add(candidate)
    if len(matching_dates) != 1:
        return ""
    return next(iter(matching_dates)).isoformat()


def _run_roofer(pointcloud: Path, footprint: Path, output: Path, settings: Settings) -> tuple[Path, Path | None]:
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    # Roofer's stable image carries a different PROJ database than the pinned
    # conda GDAL/PDAL stack. Scope Roofer's paths to this subprocess so one
    # component cannot load the other component's incompatible data files.
    environment["GDAL_DATA"] = os.getenv("ROOFER_GDAL_DATA", "/opt/roofer/share/gdal")
    environment["PROJ_DATA"] = os.getenv("ROOFER_PROJ_DATA", "/opt/roofer/share/proj")
    environment["PROJ_LIB"] = environment["PROJ_DATA"]
    _run(
        [
            "roofer",
            "--id-attribute",
            "request_id",
            "--split-cjseq",
            "--lod22",
            str(pointcloud),
            str(footprint),
            str(output),
        ],
        timeout=settings.command_timeout_seconds,
        environment=environment,
    )
    features = sorted(output.glob("**/*.city.jsonl"))
    if len(features) != 1:
        raise UnreliableGeometryError(
            "ROOFER_OUTPUT_COUNT_INVALID",
            "Roofer did not produce exactly one reconstructed building for the property.",
        )
    metadata = output / "metadata.json"
    return features[0], metadata if metadata.exists() else None


def _solar_reconciliation(geometry: dict[str, Any], request: GeometryRequest, settings: Settings) -> dict[str, float]:
    solar = request.solarReference
    area_variance = abs(float(geometry["roofAreaSqFt"]) - solar.roofAreaSqFt) / solar.roofAreaSqFt * 100
    pitch_variance = abs(float(geometry["averagePitchDegrees"]) - solar.averagePitchDegrees)
    if area_variance > settings.maximum_solar_area_variance_percent:
        raise UnreliableGeometryError(
            "SOLAR_AREA_RECONCILIATION_FAILED",
            "The open-source roof model and Google Solar roof area disagree beyond the production threshold.",
            details={"areaVariancePercent": round(area_variance, 2)},
        )
    if pitch_variance > settings.maximum_solar_pitch_variance_degrees:
        raise UnreliableGeometryError(
            "SOLAR_PITCH_RECONCILIATION_FAILED",
            "The open-source roof model and Google Solar pitch disagree beyond the production threshold.",
            details={"pitchVarianceDegrees": round(pitch_variance, 2)},
        )
    area_score = max(0.0, 1 - area_variance / settings.maximum_solar_area_variance_percent)
    pitch_score = max(0.0, 1 - pitch_variance / settings.maximum_solar_pitch_variance_degrees)
    return {
        "areaVariancePercent": round(area_variance, 2),
        "pitchVarianceDegrees": round(pitch_variance, 2),
        "confidence": round((area_score + pitch_score) / 2, 3),
    }


def _validate_selected_roof_type(geometry: dict[str, Any], request: GeometryRequest) -> None:
    flat_share = float(geometry["flatRoofAreaSqFt"]) / max(float(geometry["roofAreaSqFt"]), 0.01)
    selected = request.selectedRoofType
    if selected.flat and flat_share < 0.50:
        raise UnreliableGeometryError(
            "SELECTED_FLAT_ROOF_MISMATCH",
            "The selected flat-roof system does not agree with the reconstructed roof geometry.",
        )
    if selected.family == "STEEP_SLOPE" and flat_share > 0.50:
        raise UnreliableGeometryError(
            "SELECTED_STEEP_ROOF_MISMATCH",
            "The selected steep-slope roof system does not agree with the reconstructed roof geometry.",
        )


def _combined_confidence(
    geometry_confidence: float,
    footprint_distance_meters: float,
    lidar_age_years: int,
    reconciliation_confidence: float,
    settings: Settings,
) -> tuple[float, dict[str, float]]:
    components = {
        "rooferQuality": max(0.0, min(1.0, geometry_confidence)),
        "footprintMatch": max(
            0.0, 1 - footprint_distance_meters / max(settings.footprint_max_distance_meters, 0.01)
        ),
        "lidarAge": max(0.0, 1 - lidar_age_years / max(settings.maximum_lidar_age_years, 1)),
        "solarReconciliation": max(0.0, min(1.0, reconciliation_confidence)),
    }
    score = (
        components["rooferQuality"] * 0.55
        + components["footprintMatch"] * 0.10
        + components["lidarAge"] * 0.10
        + components["solarReconciliation"] * 0.25
    )
    return round(score, 3), {key: round(value, 3) for key, value in components.items()}


def reconstruct_roof(request: GeometryRequest, settings: Settings) -> dict[str, Any]:
    longitude = request.location.longitude
    latitude = request.location.latitude
    if not (
        settings.minimum_longitude <= longitude <= settings.maximum_longitude
        and settings.minimum_latitude <= latitude <= settings.maximum_latitude
    ):
        raise UnreliableGeometryError("OUTSIDE_SERVICE_AREA", "The property is outside the configured geometry service area.")

    with tempfile.TemporaryDirectory(prefix=f"{request.requestId}-", dir=settings.work_root) as directory:
        workspace = Path(directory)
        footprint = fetch_overture_footprint(longitude, latitude, workspace, settings)
        registries = load_registries(settings.lidar_registry_path, settings.imagery_registry_path)
        county, lidar_candidates, selection_audit = select_regional_lidar(
            footprint, longitude, latitude, settings, registries
        )
        footprint_gpkg, crop_wkt, target_epsg, projected_footprint = write_footprint_inputs(
            footprint, request.requestId, workspace, longitude, latitude, settings
        )
        attempts: list[dict[str, Any]] = []
        selected: tuple[LidarResource, dict[str, Any], float, dict[str, Any]] | None = None
        for candidate_index, candidate in enumerate(lidar_candidates):
            lidar = candidate
            pointcloud = workspace / f"roof-points-{candidate_index}.laz"
            try:
                point_audit = _pdal_crop(lidar, crop_wkt, target_epsg, pointcloud, workspace, settings)
                if point_audit["tileAcquisitionDate"]:
                    acquired = date.fromisoformat(point_audit["tileAcquisitionDate"])
                    today = datetime.now(timezone.utc).date()
                    age_years = max(
                        0,
                        today.year
                        - acquired.year
                        - ((today.month, today.day) < (acquired.month, acquired.day)),
                    )
                    lidar = replace(
                        lidar,
                        tile_acquisition_date=point_audit["tileAcquisitionDate"],
                        age_years=age_years,
                    )
                point_density = float(point_audit["filteredPointCount"]) / max(
                    float(projected_footprint.area), 0.01
                )
                required_density = max(settings.minimum_point_density, lidar.minimum_density_ppsm)
                if point_density < required_density:
                    raise UnreliableGeometryError(
                        "LIDAR_DENSITY_TOO_LOW",
                        "The selected point cloud does not meet its registered post-filter density requirement.",
                        details={
                            "sourceId": lidar.source_id,
                            "pointDensityPpsm": round(point_density, 3),
                            "minimumDensityPpsm": required_density,
                        },
                    )
                feature_path, metadata_path = _run_roofer(
                    pointcloud,
                    footprint_gpkg,
                    workspace / f"roofer-output-{candidate_index}",
                    settings,
                )
                feature, transform = load_cityjson_feature(feature_path, metadata_path)
                geometry = extract_roof_geometry(
                    feature,
                    transform,
                    flat_pitch_degrees=settings.flat_pitch_degrees,
                    minimum_density=settings.minimum_point_density,
                    maximum_nodata_fraction=settings.maximum_nodata_fraction,
                    maximum_rmse_meters=settings.maximum_roofer_rmse_meters,
                )
                reconciliation = _solar_reconciliation(geometry, request, settings)
                _validate_selected_roof_type(geometry, request)
                confidence, confidence_components = _combined_confidence(
                    float(geometry["confidence"]),
                    footprint.distance_meters,
                    lidar.age_years,
                    reconciliation["confidence"],
                    settings,
                )
                if confidence < settings.minimum_service_confidence:
                    raise UnreliableGeometryError(
                        "GEOMETRY_CONFIDENCE_TOO_LOW",
                        "The open-source roof model did not meet the automatic measurement confidence threshold.",
                        details={"confidence": confidence, "components": confidence_components},
                    )
                geometry["confidence"] = confidence
                geometry["confidenceComponents"] = confidence_components
                geometry["reconciliation"] = reconciliation
                selected = (lidar, point_audit, point_density, geometry)
                attempts.append({"sourceId": lidar.source_id, "decision": "SELECTED_VALID_CROP"})
                break
            except UnreliableGeometryError as error:
                attempts.append(
                    {
                        "sourceId": lidar.source_id,
                        "decision": "REJECTED_PROPERTY_CROP",
                        "errorCode": error.code,
                        **({"errorDetails": error.details} if error.details else {}),
                    }
                )

        selection_audit.extend(attempts)
        if selected is None:
            error_codes = {str(item.get("errorCode") or "") for item in attempts}
            code = (
                "NO_USABLE_ROOF_RETURNS"
                if error_codes and error_codes.issubset({"NO_USABLE_ROOF_RETURNS", "LIDAR_CROP_EMPTY"})
                else "NO_RELIABLE_LIDAR_GEOMETRY"
            )
            raise UnreliableGeometryError(
                code,
                "No registered LiDAR candidate produced reliable roof geometry for this property.",
                details={"selectionAttempts": attempts},
            )
        lidar, point_audit, point_density, geometry = selected
        imagery_decision = validate_current_structure(
            footprint,
            lidar,
            county,
            registries,
            current_lidar_max_age_years=settings.current_lidar_max_age_years,
            maximum_current_imagery_age_years=settings.maximum_current_imagery_age_years,
            allow_historical_verified_pricing=settings.allow_historical_verified_pricing,
            solar_reference=request.solarReference,
            reconstructed_geometry=geometry,
            maximum_area_variance_percent=settings.maximum_solar_area_variance_percent,
            maximum_pitch_variance_degrees=settings.maximum_solar_pitch_variance_degrees,
            provider_timeout_seconds=settings.provider_timeout_seconds,
        )

        lidar_component = "NOAA/DigitalCoast" if "NOAA" in lidar.provider.upper() else "USGS/3DEP"
        lidar_license_text = (
            "NOAA-PUBLIC-DATASET" if lidar_component == "NOAA/DigitalCoast" else "USGS-PUBLIC-DOMAIN"
        )

        return {
            "available": True,
            "verificationStatus": imagery_decision["verificationStatus"],
            "pricingAllowed": imagery_decision["pricingAllowed"],
            "status": imagery_decision["status"],
            "holdReason": imagery_decision["holdReason"],
            "provider": f"3DBAG Roofer + PDAL + {lidar.provider}",
            "provenance": {
                "serviceLicense": "GPL-3.0",
                "sourceCodeUrl": settings.service_source_url,
                "modelVersion": f"service:{settings.service_commit};roofer:{settings.roofer_commit}",
                "registryIds": [
                    "3DBAG/roofer",
                    "PDAL/PDAL",
                    "OvertureMaps/overturemaps-py",
                    lidar_component,
                ],
                "inputDataLicense": f"{lidar_license_text} + OVERTURE-BUILDINGS-ODBL-1.0",
                "inputImageryCommerciallyAuthorized": imagery_decision["currentImagery"].get("validation") == "PASSED",
            },
            "geometry": geometry,
            "dataSources": {
                "footprint": {
                    "provider": "Overture Maps Buildings",
                    "id": footprint.overture_id,
                    "release": footprint.overture_release,
                    "license": "ODbL-1.0",
                    "attribution": "Overture Maps Foundation and source contributors",
                    "sourceRecords": footprint.source_records[:20],
                    "geocodeDistanceMeters": round(footprint.distance_meters, 2),
                },
                "pointCloud": {
                    "provider": lidar.provider,
                    "sourceId": lidar.source_id,
                    "resource": lidar.dataset_name,
                    "metadataUrl": lidar.metadata_url,
                    "county": county,
                    "acquiredStart": lidar.acquired_start,
                    "acquiredEnd": lidar.acquired_end,
                    "tileAcquisitionDate": lidar.tile_acquisition_date,
                    "acquisitionYear": int((lidar.tile_acquisition_date or lidar.acquired_end)[:4]),
                    "ageYears": lidar.age_years,
                    "coverageRatio": lidar.coverage_ratio,
                    "pointDensityPpsm": round(point_density, 3),
                    "classHistogram": point_audit["classHistogram"],
                    "allowedClasses": point_audit["allowedClasses"],
                    "roofClasses": point_audit["roofClasses"],
                    "license": lidar.license,
                    "licenseStatus": "AUTHORIZED_PUBLIC_DATASET",
                    "selectionReason": lidar.selection_reason,
                },
                "currentImagery": imagery_decision["currentImagery"],
                "reconstruction": {
                    "provider": "3DBAG Roofer",
                    "version": settings.roofer_version,
                    "commit": settings.roofer_commit,
                    "license": "GPL-3.0",
                },
                "pointProcessing": {
                    "provider": "PDAL",
                    "version": settings.pdal_version,
                    "license": "BSD-3-Clause",
                },
                "footprintClient": {
                    "provider": "overturemaps-py",
                    "version": settings.overturemaps_version,
                    "license": "MIT",
                },
            },
            "audit": {
                "registryVersion": registries.version_hash,
                "selectionCandidates": selection_audit,
                "selectedSourceId": lidar.source_id,
                "selectedCounty": county,
                "pdal": point_audit,
                "thresholds": {
                    "minimumPointDensityPpsm": settings.minimum_point_density,
                    "sourceMinimumPointDensityPpsm": lidar.minimum_density_ppsm,
                    "minimumCoverageRatio": settings.minimum_lidar_coverage_ratio,
                    "maximumNoDataFraction": settings.maximum_nodata_fraction,
                    "maximumRooferRmseMeters": settings.maximum_roofer_rmse_meters,
                    "minimumServiceConfidence": settings.minimum_service_confidence,
                },
                "warnings": imagery_decision["warnings"],
            },
        }


def runtime_dependencies() -> dict[str, bool]:
    return {command: shutil.which(command) is not None for command in ("roofer", "pdal", "ogr2ogr", "overturemaps")}
