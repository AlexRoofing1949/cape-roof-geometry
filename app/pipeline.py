"""End-to-end open-source roof reconstruction pipeline."""

from __future__ import annotations

import importlib.util
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
from .plane_validation import validate_roofer_planes
from .providers import (
    fetch_best_footprint,
    fetch_google_solar_roofprint,
    LidarResource,
    select_regional_lidar,
    write_footprint_inputs,
)
from .source_registry import load_registries


ROOF_NORMAL_KNN = 12
MINIMUM_ROOF_NORMAL_Z = 0.65
MAXIMUM_ROOF_CURVATURE = 0.12
METERS_TO_FEET = 3.280839895013123


def _enforce_roofprint_perimeter_consistency(
    geometry: dict[str, Any],
    projected_footprint: Any,
    maximum_variance_percent: float,
) -> dict[str, float]:
    """Reject reconstructed exterior topology that cannot close to the roofprint.

    The comparison is planimetric on both sides.  It is a topology validation
    gate, never a source of inferred eave, rake, ridge, hip, or valley lengths.
    """

    roofprint_perimeter_feet = float(projected_footprint.length) * METERS_TO_FEET
    reconstructed_perimeter_feet = float(
        geometry.get("externalProjectedPerimeterFeet") or 0
    )
    if roofprint_perimeter_feet <= 0 or reconstructed_perimeter_feet <= 0:
        raise UnreliableGeometryError(
            "ROOF_TOPOLOGY_PERIMETER_MISSING",
            "The reconstructed roof exterior could not be reconciled with the selected roofprint.",
        )
    variance_percent = (
        abs(reconstructed_perimeter_feet - roofprint_perimeter_feet)
        / roofprint_perimeter_feet
        * 100
    )
    evidence = {
        "roofprintPerimeterFeet": round(roofprint_perimeter_feet, 2),
        "reconstructedProjectedPerimeterFeet": round(
            reconstructed_perimeter_feet, 2
        ),
        "variancePercent": round(variance_percent, 3),
        "maximumVariancePercent": round(maximum_variance_percent, 3),
        "topology": {
            key: (geometry.get("topology") or {}).get(key)
            for key in (
                "nodedEdgeCount",
                "sharedEdgeCount",
                "exteriorEdgeCount",
                "rejectedNodedAdjacencyCount",
                "suppressedCrossingArtifactFeet",
            )
        },
    }
    if variance_percent > maximum_variance_percent:
        raise UnreliableGeometryError(
            "ROOF_TOPOLOGY_PERIMETER_MISMATCH",
            "The reconstructed roof exterior does not close to the selected roofprint within the pricing-safe tolerance.",
            details=evidence,
        )
    geometry["roofprintPerimeterReconciliation"] = evidence
    return evidence


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
    roofer_class_assignments = [
        f"Classification = 6 WHERE Classification == {classification}"
        for classification in lidar.roof_classes
        if classification != 6
    ]
    metadata_path = workspace / "pdal-metadata.json"
    class_one_preprocessing: list[dict[str, Any]] = []
    if 1 in lidar.roof_classes:
        class_one_preprocessing = [
            {
                "type": "filters.hag_delaunay",
                "count": 8,
                "allow_extrapolation": False,
            },
            {
                "type": "filters.expression",
                "expression": (
                    "Classification == 6 || "
                    f"(Classification == 1 && HeightAboveGround >= {settings.minimum_roof_hag_meters} "
                    f"&& HeightAboveGround <= {settings.maximum_roof_hag_meters})"
                ),
            },
            {
                "type": "filters.cluster",
                "tolerance": settings.roof_cluster_tolerance_meters,
                "min_points": settings.minimum_roof_cluster_points,
                "is3d": True,
            },
            {
                "type": "filters.expression",
                "expression": "ClusterID > 0",
            },
            {
                "type": "filters.normal",
                "knn": ROOF_NORMAL_KNN,
                "always_up": True,
            },
            {
                "type": "filters.expression",
                "expression": (
                    "Classification == 6 || "
                    f"(Classification == 1 && NormalZ >= {MINIMUM_ROOF_NORMAL_Z} "
                    f"&& Curvature <= {MAXIMUM_ROOF_CURVATURE})"
                ),
            },
        ]
    pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": lidar.ept_url,
                "polygon": f"{crop_wkt_wgs84}/EPSG:4326",
                "requests": 8,
            },
            {
                "type": "filters.reprojection",
                "out_srs": f"EPSG:{target_epsg}",
            },
            {
                # EPT nodes are fetched concurrently, so their arrival order is
                # not a stable reconstruction input.  Canonical XY ordering
                # makes every downstream neighbourhood/cluster calculation and
                # Roofer invocation consume the same point sequence.
                "type": "filters.mortonorder",
                "reverse": False,
            },
            {
                "type": "filters.expression",
                "expression": class_expression,
            },
            {
                "type": "filters.outlier",
                "method": "statistical",
                "mean_k": 12,
                "multiplier": 2.2,
                "where": "Classification == 1 || Classification == 6",
                "where_merge": True,
            },
            {
                "type": "filters.expression",
                "expression": "Classification != 7",
            },
            *class_one_preprocessing,
            {
                "type": "filters.stats",
                "dimensions": (
                    "Classification,GpsTime,HeightAboveGround,ClusterID,NormalZ,Curvature"
                    if class_one_preprocessing
                    else "Classification,GpsTime"
                ),
                "count": "Classification",
            },
            *(
                [{"type": "filters.assign", "value": roofer_class_assignments}]
                if roofer_class_assignments
                else []
            ),
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
        "rooferClassNormalization": roofer_class_assignments,
        "pointOrder": {
            "provider": "PDAL filters.mortonorder",
            "method": "XY Morton order",
            "reverse": False,
        },
        "noiseFilter": {
            "provider": "PDAL filters.outlier",
            "method": "statistical",
            "meanK": 12,
            "multiplier": 2.2,
            "outlierClassRemoved": 7,
        },
        "classOneCorrection": {
            "applied": bool(class_one_preprocessing),
            "method": (
                "PDAL HAG + height filter + 3D cluster noise rejection + "
                "surface-normal/curvature filtering for unclassified returns"
            ),
            "minimumHeightAboveGroundMeters": settings.minimum_roof_hag_meters,
            "maximumHeightAboveGroundMeters": settings.maximum_roof_hag_meters,
            "clusterToleranceMeters": settings.roof_cluster_tolerance_meters,
            "minimumClusterPoints": settings.minimum_roof_cluster_points,
            "normalKnn": ROOF_NORMAL_KNN,
            "minimumNormalZ": MINIMUM_ROOF_NORMAL_Z,
            "maximumCurvature": MAXIMUM_ROOF_CURVATURE,
        },
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
            "--jobs",
            "1",
            "--id-attribute",
            "request_id",
            "--split-cjseq",
            "--lod22",
            "--plane-detect-epsilon",
            str(settings.roofer_plane_detect_epsilon_meters),
            "--complexity-factor",
            str(settings.roofer_complexity_factor),
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
    *,
    current_structure_validated: bool = False,
) -> tuple[float, dict[str, float]]:
    lidar_age_component = max(0.0, 1 - lidar_age_years / max(settings.maximum_lidar_age_years, 1))
    components = {
        "rooferQuality": max(0.0, min(1.0, geometry_confidence)),
        "footprintMatch": max(
            0.0, 1 - footprint_distance_meters / max(settings.footprint_max_distance_meters, 0.01)
        ),
        "lidarAge": lidar_age_component,
        # Historical geometry is not trusted merely because it exists. A
        # registered, newer current-structure comparison must pass before the
        # temporal component can receive full credit.
        "temporalVerification": 1.0 if current_structure_validated else lidar_age_component,
        "solarReconciliation": max(0.0, min(1.0, reconciliation_confidence)),
    }
    score = (
        components["rooferQuality"] * 0.55
        + components["footprintMatch"] * 0.10
        + components["temporalVerification"] * 0.10
        + components["solarReconciliation"] * 0.25
    )
    return round(score, 3), {key: round(value, 3) for key, value in components.items()}


def _confidence_diagnostics(
    geometry: dict[str, Any],
    reconciliation: dict[str, Any],
    imagery_decision: dict[str, Any],
    lidar: LidarResource,
    point_audit: dict[str, Any],
    point_density: float,
) -> dict[str, Any]:
    """Return calibration-safe failure evidence without vertices or credentials."""

    current_imagery = imagery_decision.get("currentImagery") or {}
    return {
        "geometry": {
            key: geometry.get(key)
            for key in (
                "roofAreaSqFt",
                "averagePitchDegrees",
                "maximumPitchDegrees",
                "rakesFeet",
                "eavesFeet",
                "valleysFeet",
                "ridgesFeet",
                "hipsFeet",
                "highPerimeterFeet",
                "flatRoofAreaSqFt",
                "externalPerimeterFeet",
                "internalRoofEdgeFeet",
            )
        }
        | {
            "facetCount": len(geometry.get("facets") or []),
            "quality": geometry.get("quality") or {},
            "independentPlaneValidation": (
                (geometry.get("independentPlaneValidation") or {}).get("validation")
            ),
        },
        "reconciliation": reconciliation,
        "currentStructure": {
            "verificationStatus": imagery_decision.get("verificationStatus"),
            "pricingAllowed": bool(imagery_decision.get("pricingAllowed")),
            "status": imagery_decision.get("status"),
            "currentImagery": {
                key: current_imagery.get(key)
                for key in (
                    "sourceId",
                    "captureDate",
                    "validation",
                    "unchanged",
                    "method",
                    "failureReasons",
                )
            },
            "warnings": imagery_decision.get("warnings") or [],
        },
        "pointCloud": {
            "sourceId": lidar.source_id,
            "tileAcquisitionDate": point_audit.get("tileAcquisitionDate") or "",
            "pointDensityPpsm": round(point_density, 3),
        },
    }


def _enforce_lidar_acquisition_floor(
    lidar: LidarResource, tile_acquisition_date: str, settings: Settings
) -> date | None:
    """Reject an exact property-crop date older than the approved fixed floor."""

    if not tile_acquisition_date:
        return None
    acquired = date.fromisoformat(tile_acquisition_date)
    if acquired < settings.minimum_lidar_acquisition_date:
        raise UnreliableGeometryError(
            "LIDAR_TILE_BEFORE_MINIMUM_ACQUISITION_DATE",
            "The property crop predates the approved LiDAR acquisition floor.",
            details={
                "sourceId": lidar.source_id,
                "tileAcquisitionDate": acquired.isoformat(),
                "minimumAcquisitionDate": settings.minimum_lidar_acquisition_date.isoformat(),
            },
        )
    return acquired


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
        building_footprint = fetch_best_footprint(longitude, latitude, workspace, settings)
        footprint = fetch_google_solar_roofprint(
            building_footprint,
            longitude,
            latitude,
            workspace,
            settings,
            request.solarReference,
        )
        registries = load_registries(settings.lidar_registry_path, settings.imagery_registry_path)
        county, lidar_candidates, selection_audit = select_regional_lidar(
            footprint, longitude, latitude, settings, registries
        )
        footprint_gpkg, crop_wkt, target_epsg, projected_footprint = write_footprint_inputs(
            footprint, request.requestId, workspace, longitude, latitude, settings
        )
        attempts: list[dict[str, Any]] = []
        selected: tuple[LidarResource, dict[str, Any], float, dict[str, Any], dict[str, Any]] | None = None
        for candidate_index, candidate in enumerate(lidar_candidates):
            lidar = candidate
            pointcloud = workspace / f"roof-points-{candidate_index}.laz"
            try:
                point_audit = _pdal_crop(lidar, crop_wkt, target_epsg, pointcloud, workspace, settings)
                if point_audit["tileAcquisitionDate"]:
                    acquired = _enforce_lidar_acquisition_floor(
                        lidar, point_audit["tileAcquisitionDate"], settings
                    )
                    assert acquired is not None
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
                    edge_node_tolerance_meters=settings.roof_edge_node_tolerance_meters,
                    edge_node_vertical_tolerance_meters=(
                        settings.roof_edge_vertical_node_tolerance_meters
                    ),
                    plane_intersection_maximum_displacement_meters=(
                        settings.roof_plane_intersection_maximum_displacement_meters
                    ),
                    shared_edge_maximum_direction_variance_degrees=(
                        settings.roof_shared_edge_maximum_direction_variance_degrees
                    ),
                    minimum_density=settings.minimum_point_density,
                    maximum_nodata_fraction=settings.maximum_nodata_fraction,
                    maximum_rmse_meters=settings.maximum_roofer_rmse_meters,
                    include_validation_facets=True,
                )
                _enforce_roofprint_perimeter_consistency(
                    geometry,
                    projected_footprint,
                    settings.maximum_roofprint_perimeter_variance_percent,
                )
                validation_facets = geometry.pop("_validationFacets")
                plane_validation = validate_roofer_planes(
                    pointcloud, validation_facets, workspace, settings
                )
                geometry["independentPlaneValidation"] = plane_validation
                reconciliation = _solar_reconciliation(geometry, request, settings)
                _validate_selected_roof_type(geometry, request)
                imagery_decision = validate_current_structure(
                    building_footprint,
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
                current_structure_validated = (
                    imagery_decision.get("verificationStatus")
                    in {"VERIFIED_CURRENT", "VERIFIED_HISTORICAL_UNCHANGED"}
                    and (imagery_decision.get("currentImagery") or {}).get("validation") == "PASSED"
                )
                confidence, confidence_components = _combined_confidence(
                    float(geometry["confidence"]),
                    footprint.distance_meters,
                    lidar.age_years,
                    reconciliation["confidence"],
                    settings,
                    current_structure_validated=current_structure_validated,
                )
                if confidence < settings.minimum_service_confidence:
                    diagnostics = _confidence_diagnostics(
                        geometry,
                        reconciliation,
                        imagery_decision,
                        lidar,
                        point_audit,
                        point_density,
                    )
                    raise UnreliableGeometryError(
                        "GEOMETRY_CONFIDENCE_TOO_LOW",
                        "The open-source roof model did not meet the automatic measurement confidence threshold.",
                        details={
                            "confidence": confidence,
                            "components": confidence_components,
                            **diagnostics,
                        },
                    )
                geometry["confidence"] = confidence
                geometry["confidenceComponents"] = confidence_components
                geometry["reconciliation"] = reconciliation
                selected = (lidar, point_audit, point_density, geometry, imagery_decision)
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
        lidar, point_audit, point_density, geometry, imagery_decision = selected

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
                    "isl-org/Open3D",
                    building_footprint.provider,
                    footprint.provider,
                    lidar_component,
                ],
                "inputDataLicense": (
                    f"{lidar_license_text} + {building_footprint.license} + {footprint.license}"
                ),
                "inputImageryCommerciallyAuthorized": imagery_decision["currentImagery"].get("validation") == "PASSED",
            },
            "geometry": geometry,
            "dataSources": {
                "footprint": {
                    "provider": footprint.provider,
                    "id": footprint.overture_id,
                    "release": footprint.overture_release,
                    "license": footprint.license,
                    "attribution": footprint.attribution,
                    "sourceRecords": footprint.source_records[:20],
                    "geocodeDistanceMeters": round(footprint.distance_meters, 2),
                    "consensusStatus": footprint.consensus_status,
                    "consensusRecords": list(footprint.consensus_records),
                },
                "buildingFootprint": {
                    "provider": building_footprint.provider,
                    "id": building_footprint.overture_id,
                    "release": building_footprint.overture_release,
                    "license": building_footprint.license,
                    "attribution": building_footprint.attribution,
                    "sourceRecords": building_footprint.source_records[:20],
                    "geocodeDistanceMeters": round(building_footprint.distance_meters, 2),
                    "consensusStatus": building_footprint.consensus_status,
                    "consensusRecords": list(building_footprint.consensus_records),
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
                    "rooferClassNormalization": point_audit["rooferClassNormalization"],
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
                "planeValidation": geometry["independentPlaneValidation"],
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
                    "rooferPlaneDetectEpsilonMeters": (
                        settings.roofer_plane_detect_epsilon_meters
                    ),
                    "rooferComplexityFactor": settings.roofer_complexity_factor,
                    "minimumServiceConfidence": settings.minimum_service_confidence,
                },
                "warnings": imagery_decision["warnings"],
            },
        }


def runtime_dependencies() -> dict[str, bool]:
    dependencies = {
        command: shutil.which(command) is not None
        for command in ("roofer", "pdal", "ogr2ogr", "overturemaps")
    }
    try:
        open3d = importlib.import_module("open3d")
        dependencies["open3d"] = bool(getattr(open3d, "__version__", ""))
    except (ImportError, OSError):
        dependencies["open3d"] = False
    try:
        gdal = importlib.import_module("osgeo.gdal")
        dependencies["gdalPython"] = bool(gdal.VersionInfo())
    except (ImportError, OSError):
        dependencies["gdalPython"] = False
    return dependencies
