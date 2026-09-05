"""Fail-closed validation of registered, commercially authorized current imagery evidence."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

from shapely.affinity import translate
from shapely.geometry import shape

from .providers import FootprintResult, LidarResource, transform_geometry, utm_epsg
from .source_registry import PENDING_LICENSE_MARKERS, RegistryBundle


SQUARE_METERS_TO_SQUARE_FEET = 10.763910416709722

# The Lee County layer is an official wall/building footprint extracted from
# the current aerial program, while the reconstruction footprint can be a roof
# mask that includes overhangs.  Raw overlap remains mandatory so a nearby
# building cannot be substituted.  A small, bounded centroid alignment then
# distinguishes systematic orthophoto/georegistration offset from a material
# footprint-shape change.
ARCGIS_MINIMUM_RAW_IOU = 0.65
ARCGIS_MINIMUM_ALIGNED_IOU = 0.85
ARCGIS_MAXIMUM_CENTROID_SHIFT_METERS = 4.0
ARCGIS_MAXIMUM_AREA_CHANGE_PERCENT = 16.0

_CALIBRATION_THRESHOLDS = {
    "polygonIou": (0.90, "minimum"),
    "boundaryF1": (0.90, "minimum"),
    "medianAreaErrorPercent": (5.0, "maximum"),
    "additionDeletionPrecision": (0.90, "minimum"),
    "additionDeletionRecall": (0.90, "minimum"),
    "falseChangeRatePercent": (5.0, "maximum"),
    "failureRatePercent": (5.0, "maximum"),
}


def _imagery_evidence_calibration_failures(record: dict[str, Any]) -> list[str]:
    """Reject model-produced imagery evidence that lacks production calibration."""

    failures: list[str] = []
    identity_fields = {
        "modelName": "MODEL_NAME",
        "modelVersion": "MODEL_VERSION",
        "calibrationDatasetVersion": "DATASET_VERSION",
    }
    for field, code in identity_fields.items():
        if not str(record.get(field) or "").strip():
            failures.append(f"CALIBRATION_{code}_MISSING")
    processing_fields = {
        "orthorectified": "ORTHORECTIFICATION",
        "coregistered": "COREGISTRATION",
        "shadowVegetationMasked": "SHADOW_VEGETATION_MASK",
    }
    for field, code in processing_fields.items():
        if record.get(field) is not True:
            failures.append(f"CALIBRATION_{code}_FAILED")

    metrics = record.get("calibrationMetrics")
    if not isinstance(metrics, dict):
        return failures + ["CALIBRATION_METRICS_MISSING"]
    for name, (threshold, direction) in _CALIBRATION_THRESHOLDS.items():
        try:
            value = float(metrics[name])
        except (KeyError, TypeError, ValueError):
            failures.append(f"CALIBRATION_{name.upper()}_MISSING")
            continue
        if direction == "minimum" and value < threshold:
            failures.append(f"CALIBRATION_{name.upper()}_BELOW_THRESHOLD")
        elif direction == "maximum" and value > threshold:
            failures.append(f"CALIBRATION_{name.upper()}_ABOVE_THRESHOLD")
    return failures


def _alignment_and_change(reference, current) -> tuple[float, float, float, float]:
    longitude = float(reference.centroid.x)
    latitude = float(reference.centroid.y)
    epsg = utm_epsg(longitude, latitude)
    reference_m = transform_geometry(reference, "EPSG:4326", f"EPSG:{epsg}")
    current_m = transform_geometry(current, "EPSG:4326", f"EPSG:{epsg}")
    union = reference_m.union(current_m).area
    iou = reference_m.intersection(current_m).area / max(union, 0.01)
    shift = reference_m.centroid.distance(current_m.centroid)
    aligned_current_m = translate(
        current_m,
        xoff=reference_m.centroid.x - current_m.centroid.x,
        yoff=reference_m.centroid.y - current_m.centroid.y,
    )
    aligned_union = reference_m.union(aligned_current_m).area
    aligned_iou = (
        reference_m.intersection(aligned_current_m).area
        / max(aligned_union, 0.01)
    )
    area_change = abs(current_m.area - reference_m.area) / max(reference_m.area, 0.01) * 100
    return float(iou), float(aligned_iou), float(shift), float(area_change)


def _distance_and_change(reference, current) -> tuple[float, float, float]:
    iou, _aligned_iou, shift, area_change = _alignment_and_change(
        reference, current
    )
    return iou, shift, area_change


def _year_age(value: date) -> int:
    today = datetime.now(timezone.utc).date()
    return max(0, today.year - value.year - ((today.month, today.day) < (value.month, value.day)))


def _historical_verification_outcome(
    current_imagery: dict[str, Any],
    lidar: LidarResource,
    *,
    current_lidar_max_age_years: int,
    allow_historical_verified_pricing: bool,
    warning: str,
) -> dict[str, Any]:
    if lidar.age_years <= current_lidar_max_age_years:
        verification_status = "VERIFIED_CURRENT"
        pricing_allowed = True
    else:
        verification_status = "VERIFIED_HISTORICAL_UNCHANGED"
        pricing_allowed = allow_historical_verified_pricing
    return {
        "verificationStatus": verification_status,
        "pricingAllowed": pricing_allowed,
        "status": "GEOMETRY_VERIFIED" if pricing_allowed else "HISTORICAL_PRICING_CALIBRATION_HOLD",
        "holdReason": "" if pricing_allowed else "HISTORICAL_PRICING_DISABLED_DURING_CALIBRATION",
        "currentImagery": current_imagery,
        "warnings": [] if pricing_allowed else [warning],
    }


def _arcgis_building_validation(
    footprint: FootprintResult,
    lidar: LidarResource,
    source: Any,
    *,
    provider_timeout_seconds: int,
    maximum_current_imagery_age_years: int,
    current_lidar_max_age_years: int,
    allow_historical_verified_pricing: bool,
) -> dict[str, Any]:
    min_x, min_y, max_x, max_y = footprint.geometry_wgs84.bounds
    query = urllib.parse.urlencode(
        {
            "where": "1=1",
            "geometry": f"{min_x},{min_y},{max_x},{max_y}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "OBJECTID,BldgDataSource,ModifyDate,last_edited_date",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        }
    )
    request = urllib.request.Request(
        f"{source.evidence_endpoint}?{query}", headers={"User-Agent": "CapeRoofGeometry/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=provider_timeout_seconds) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_INSUFFICIENT",
            "holdReason": "CURRENT_BUILDING_EVIDENCE_UNAVAILABLE",
            "currentImagery": {
                "sourceId": source.id,
                "captureDate": source.capture_end.isoformat(),
                "validation": "FAILED",
            },
            "warnings": ["CURRENT_BUILDING_EVIDENCE_UNAVAILABLE"],
        }

    matches: list[tuple[float, float, float, float, dict[str, Any]]] = []
    for feature in payload.get("features") or []:
        try:
            current = shape(feature.get("geometry"))
            if current.is_empty or not current.is_valid:
                continue
            iou, aligned_iou, shift, area_change = _alignment_and_change(
                footprint.geometry_wgs84, current
            )
            matches.append(
                (
                    iou,
                    aligned_iou,
                    shift,
                    area_change,
                    feature.get("properties") or {},
                )
            )
        except Exception:
            continue
    matches.sort(
        key=lambda item: (item[0], item[1], -item[2], -item[3]),
        reverse=True,
    )
    if not matches:
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "STRUCTURE_CHANGED_AFTER_LIDAR",
            "holdReason": "CURRENT_BUILDING_FOOTPRINT_MISSING",
            "currentImagery": {
                "sourceId": source.id,
                "captureDate": source.capture_end.isoformat(),
                "validation": "FAILED",
            },
            "warnings": ["CURRENT_BUILDING_FOOTPRINT_MISSING"],
        }

    iou, aligned_iou, centroid_shift, area_change, properties = matches[0]
    failures: list[str] = []
    lidar_reference_date = date.fromisoformat(lidar.tile_acquisition_date or lidar.acquired_end)
    if source.capture_end <= lidar_reference_date:
        failures.append("IMAGERY_NOT_NEWER_THAN_LIDAR")
    if _year_age(source.capture_end) > maximum_current_imagery_age_years:
        failures.append("IMAGERY_TOO_OLD_FOR_CURRENT_VALIDATION")
    if source.gsd_meters <= 0 or source.gsd_meters > 0.15:
        failures.append("IMAGERY_GSD_INSUFFICIENT")
    if iou < ARCGIS_MINIMUM_RAW_IOU:
        failures.append("FOOTPRINT_IOU_FAILED")
    if aligned_iou < ARCGIS_MINIMUM_ALIGNED_IOU:
        failures.append("FOOTPRINT_SHAPE_CHANGED")
    if centroid_shift > ARCGIS_MAXIMUM_CENTROID_SHIFT_METERS:
        failures.append("FOOTPRINT_CENTROID_SHIFTED")
    if area_change > ARCGIS_MAXIMUM_AREA_CHANGE_PERCENT:
        failures.append("FOOTPRINT_AREA_CHANGED")

    updated_millis = properties.get("last_edited_date") or properties.get("ModifyDate")
    try:
        evidence_updated = datetime.fromtimestamp(float(updated_millis) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        evidence_updated = ""
    current_imagery = {
        "sourceId": source.id,
        "captureDate": source.capture_end.isoformat(),
        "captureDatePrecision": "PROJECT_WINDOW_END",
        "captureStart": source.capture_start.isoformat(),
        "gsdMeters": source.gsd_meters,
        "license": source.license,
        "attribution": source.attribution,
        "imageryEndpoint": source.imagery_endpoint,
        "evidenceEndpoint": source.evidence_endpoint,
        "providerFeatureId": str(properties.get("OBJECTID") or ""),
        "providerBuildingSource": str(properties.get("BldgDataSource") or ""),
        "evidenceUpdatedDate": evidence_updated,
        "footprintIou": round(iou, 4),
        "centroidAlignedFootprintIou": round(aligned_iou, 4),
        "centroidShiftMeters": round(centroid_shift, 3),
        "areaChangePercent": round(area_change, 3),
        "comparisonThresholds": {
            "minimumRawFootprintIou": ARCGIS_MINIMUM_RAW_IOU,
            "minimumCentroidAlignedFootprintIou": (
                ARCGIS_MINIMUM_ALIGNED_IOU
            ),
            "maximumCentroidShiftMeters": (
                ARCGIS_MAXIMUM_CENTROID_SHIFT_METERS
            ),
            "maximumAreaChangePercent": ARCGIS_MAXIMUM_AREA_CHANGE_PERCENT,
        },
        "validationMethod": "OFFICIAL_BUILDING_FOOTPRINT_FROM_CURRENT_AERIAL_PROGRAM",
        "validation": "FAILED" if failures else "PASSED",
    }
    if failures:
        changed = any(
            item in failures
            for item in (
                "FOOTPRINT_IOU_FAILED",
                "FOOTPRINT_SHAPE_CHANGED",
                "FOOTPRINT_CENTROID_SHIFTED",
                "FOOTPRINT_AREA_CHANGED",
            )
        )
        status = "STRUCTURE_CHANGED_AFTER_LIDAR" if changed else "CURRENT_IMAGERY_INSUFFICIENT"
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": status,
            "holdReason": status,
            "currentImagery": current_imagery,
            "warnings": failures,
        }
    return _historical_verification_outcome(
        current_imagery,
        lidar,
        current_lidar_max_age_years=current_lidar_max_age_years,
        allow_historical_verified_pricing=allow_historical_verified_pricing,
        warning="Historical LiDAR passed official current-building checks but pricing remains disabled.",
    )


def _solar_model_validation(
    footprint: FootprintResult,
    lidar: LidarResource,
    solar_reference: Any,
    reconstructed_geometry: dict[str, Any] | None,
    *,
    maximum_current_imagery_age_years: int,
    maximum_area_variance_percent: float,
    maximum_pitch_variance_degrees: float,
    current_lidar_max_age_years: int,
    allow_historical_verified_pricing: bool,
) -> dict[str, Any]:
    """Use Google Solar's imagery-derived building model as change evidence.

    This consumes only Building Insights values already supplied by the caller;
    it does not fetch, cache, or redistribute Google imagery.  The provider's
    imagery date is explicitly recorded as approximate rather than asserted to
    be an exact aerial capture timestamp.
    """

    base = {
        "sourceId": "google_solar_building_insights",
        "captureDate": str(getattr(solar_reference, "imageryDate", "") or ""),
        "captureDatePrecision": "PROVIDER_APPROXIMATE_DAY",
        "imageryQuality": str(getattr(solar_reference, "imageryQuality", "") or "").upper(),
        "license": "GOOGLE_MAPS_PLATFORM_TERMS",
        "attribution": "Includes data from Google Maps",
        "validationMethod": "BUILDING_MODEL_RECONCILIATION",
    }

    try:
        imagery_date = date.fromisoformat(base["captureDate"])
        lidar_date = date.fromisoformat(lidar.tile_acquisition_date or lidar.acquired_end)
    except (TypeError, ValueError):
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_INSUFFICIENT",
            "holdReason": "SOLAR_IMAGERY_DATE_INVALID",
            "currentImagery": {**base, "validation": "FAILED"},
            "warnings": ["SOLAR_IMAGERY_DATE_INVALID"],
        }

    facets = list(getattr(solar_reference, "facets", []) or [])
    ground_area_sqft = sum(float(getattr(facet, "groundAreaSqFt", 0) or 0) for facet in facets)
    target_epsg = utm_epsg(
        float(footprint.geometry_wgs84.centroid.x), float(footprint.geometry_wgs84.centroid.y)
    )
    footprint_m = transform_geometry(footprint.geometry_wgs84, "EPSG:4326", f"EPSG:{target_epsg}")
    footprint_area_sqft = float(footprint_m.area) * SQUARE_METERS_TO_SQUARE_FEET
    footprint_area_variance = (
        abs(ground_area_sqft - footprint_area_sqft) / max(footprint_area_sqft, 0.01) * 100
    )

    geometry = reconstructed_geometry or {}
    solar_roof_area = float(getattr(solar_reference, "roofAreaSqFt", 0) or 0)
    solar_pitch = float(getattr(solar_reference, "averagePitchDegrees", 0) or 0)
    geometry_roof_area = float(geometry.get("roofAreaSqFt") or 0)
    geometry_pitch = float(geometry.get("averagePitchDegrees") or 0)
    roof_area_variance = abs(geometry_roof_area - solar_roof_area) / max(solar_roof_area, 0.01) * 100
    pitch_variance = abs(geometry_pitch - solar_pitch)

    failures: list[str] = []
    if imagery_date <= lidar_date:
        failures.append("IMAGERY_NOT_NEWER_THAN_LIDAR")
    if _year_age(imagery_date) > maximum_current_imagery_age_years:
        failures.append("IMAGERY_TOO_OLD_FOR_CURRENT_VALIDATION")
    if base["imageryQuality"] != "HIGH":
        failures.append("IMAGERY_QUALITY_FAILED")
    if ground_area_sqft <= 0 or footprint_area_sqft <= 0:
        failures.append("SOLAR_GROUND_AREA_MISSING")
    elif footprint_area_variance > maximum_area_variance_percent:
        failures.append("FOOTPRINT_AREA_CHANGED")
    if solar_roof_area <= 0 or geometry_roof_area <= 0:
        failures.append("ROOF_AREA_COMPARISON_MISSING")
    elif roof_area_variance > maximum_area_variance_percent:
        failures.append("ROOF_AREA_CHANGED")
    if pitch_variance > maximum_pitch_variance_degrees:
        failures.append("ROOF_PITCH_CHANGED")

    current_imagery = {
        **base,
        "captureDate": imagery_date.isoformat(),
        "imageryAgeYears": _year_age(imagery_date),
        "solarGroundAreaSqFt": round(ground_area_sqft, 2),
        "overtureFootprintAreaSqFt": round(footprint_area_sqft, 2),
        "footprintAreaVariancePercent": round(footprint_area_variance, 2),
        "solarRoofAreaSqFt": round(solar_roof_area, 2),
        "lidarRoofAreaSqFt": round(geometry_roof_area, 2),
        "roofAreaVariancePercent": round(roof_area_variance, 2),
        "pitchVarianceDegrees": round(pitch_variance, 2),
        "solarFacetCount": len(facets),
        "lidarFacetCount": len(geometry.get("facets") or []),
        "validation": "FAILED" if failures else "PASSED",
    }
    if failures:
        changed = any(
            item in failures
            for item in (
                "FOOTPRINT_AREA_CHANGED",
                "ROOF_AREA_CHANGED",
                "ROOF_PITCH_CHANGED",
            )
        )
        status = "STRUCTURE_CHANGED_AFTER_LIDAR" if changed else "CURRENT_IMAGERY_INSUFFICIENT"
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": status,
            "holdReason": status,
            "currentImagery": current_imagery,
            "warnings": failures,
        }

    return _historical_verification_outcome(
        current_imagery,
        lidar,
        current_lidar_max_age_years=current_lidar_max_age_years,
        allow_historical_verified_pricing=allow_historical_verified_pricing,
        warning="Historical LiDAR passed building-model change checks but pricing remains disabled.",
    )


def _validate_current_structure_impl(
    footprint: FootprintResult,
    lidar: LidarResource,
    county: str,
    registries: RegistryBundle,
    *,
    current_lidar_max_age_years: int,
    maximum_current_imagery_age_years: int,
    allow_historical_verified_pricing: bool,
    solar_reference: Any = None,
    reconstructed_geometry: dict[str, Any] | None = None,
    maximum_area_variance_percent: float = 15,
    maximum_pitch_variance_degrees: float = 10,
    provider_timeout_seconds: int = 45,
) -> dict[str, Any]:
    """Validate immutable evidence created from an authorized orthophoto workflow.

    The service never scrapes map viewers. An imagery source becomes usable only
    after its registry entry is enabled and its evidence file is packaged with
    the immutable service version.
    """

    # Prefer the unique GPS date recovered from the property crop.  Some public
    # EPT/LAS collections strip that dimension, while still publishing a
    # registered acquisition window.  Using the window end is conservative for
    # change detection: only evidence newer than the latest possible collection
    # day can verify that the building remained unchanged.  The public wrapper
    # below preserves this non-exact provenance in every response.
    lidar_reference_date = date.fromisoformat(lidar.tile_acquisition_date or lidar.acquired_end)

    eligible = [
        source
        for source in registries.imagery_sources
        if source.enabled
        and county in source.counties
        and source.commercial_estimate_use_allowed
        and source.license.upper() not in PENDING_LICENSE_MARKERS
        and source.capture_end > lidar_reference_date
        and (
            source.evidence_file is not None
            and source.evidence_file.is_file()
            or source.evidence_kind == "arcgis_building_footprints"
            and bool(source.evidence_endpoint)
        )
    ]
    eligible.sort(key=lambda source: source.capture_end, reverse=True)
    if not eligible:
        if solar_reference is not None:
            return _solar_model_validation(
                footprint,
                lidar,
                solar_reference,
                reconstructed_geometry,
                maximum_current_imagery_age_years=maximum_current_imagery_age_years,
                maximum_area_variance_percent=maximum_area_variance_percent,
                maximum_pitch_variance_degrees=maximum_pitch_variance_degrees,
                current_lidar_max_age_years=current_lidar_max_age_years,
                allow_historical_verified_pricing=allow_historical_verified_pricing,
            )
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_UNAVAILABLE",
            "holdReason": "CURRENT_IMAGERY_UNAVAILABLE",
            "currentImagery": {"sourceId": "", "captureDate": "", "validation": "NOT_RUN"},
            "warnings": ["No enabled, authorized current-imagery evidence covers this county."],
        }

    source = eligible[0]
    if source.evidence_kind == "arcgis_building_footprints":
        county_result = _arcgis_building_validation(
            footprint,
            lidar,
            source,
            provider_timeout_seconds=provider_timeout_seconds,
            maximum_current_imagery_age_years=maximum_current_imagery_age_years,
            current_lidar_max_age_years=current_lidar_max_age_years,
            allow_historical_verified_pricing=allow_historical_verified_pricing,
        )
        if county_result.get("pricingAllowed") or solar_reference is None:
            return county_result
        solar_result = _solar_model_validation(
            footprint,
            lidar,
            solar_reference,
            reconstructed_geometry,
            maximum_current_imagery_age_years=maximum_current_imagery_age_years,
            maximum_area_variance_percent=maximum_area_variance_percent,
            maximum_pitch_variance_degrees=maximum_pitch_variance_degrees,
            current_lidar_max_age_years=current_lidar_max_age_years,
            allow_historical_verified_pricing=allow_historical_verified_pricing,
        )
        if solar_result.get("pricingAllowed"):
            warnings = list(solar_result.get("warnings") or [])
            warnings.append("COUNTY_BUILDING_EVIDENCE_REJECTED_SOLAR_MODEL_USED")
            solar_result["warnings"] = warnings
            solar_result["alternateEvidence"] = {
                "sourceId": (county_result.get("currentImagery") or {}).get(
                    "sourceId", source.id
                ),
                "status": county_result.get("status"),
                "validation": (county_result.get("currentImagery") or {}).get(
                    "validation"
                ),
                "warnings": list(county_result.get("warnings") or []),
            }
            return solar_result
        county_result["alternateEvidence"] = {
            "sourceId": (solar_result.get("currentImagery") or {}).get(
                "sourceId", "google_solar_building_insights"
            ),
            "status": solar_result.get("status"),
            "validation": (solar_result.get("currentImagery") or {}).get(
                "validation"
            ),
            "warnings": list(solar_result.get("warnings") or []),
        }
        return county_result
    try:
        payload = json.loads(source.evidence_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_INSUFFICIENT",
            "holdReason": "CURRENT_IMAGERY_EVIDENCE_INVALID",
            "currentImagery": {"sourceId": source.id, "captureDate": source.capture_end.isoformat(), "validation": "FAILED"},
            "warnings": ["The registered current-imagery evidence file is unreadable."],
        }

    records = payload.get("records") if isinstance(payload, dict) else None
    record = records.get(footprint.overture_id) if isinstance(records, dict) else None
    if not isinstance(record, dict):
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_UNAVAILABLE",
            "holdReason": "CURRENT_IMAGERY_PROPERTY_NOT_VALIDATED",
            "currentImagery": {"sourceId": source.id, "captureDate": source.capture_end.isoformat(), "validation": "NOT_AVAILABLE"},
            "warnings": ["Current imagery has not been validated for this building."],
        }

    try:
        capture_date = date.fromisoformat(str(record.get("captureDate")))
        current_mask = shape(record.get("currentBuildingMask"))
        coverage_mask = shape(record.get("imageryCoverage"))
        iou, centroid_shift, area_change = _distance_and_change(footprint.geometry_wgs84, current_mask)
        target_epsg = utm_epsg(float(footprint.geometry_wgs84.centroid.x), float(footprint.geometry_wgs84.centroid.y))
        context = transform_geometry(footprint.geometry_wgs84, "EPSG:4326", f"EPSG:{target_epsg}").buffer(10)
        coverage = transform_geometry(coverage_mask, "EPSG:4326", f"EPSG:{target_epsg}")
        context_coverage = coverage.intersection(context).area / max(context.area, 0.01)
        quality_passed = bool(record.get("qualityPassed"))
        material_change = bool(record.get("materialChangeDetected"))
    except Exception:
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_INSUFFICIENT",
            "holdReason": "CURRENT_IMAGERY_EVIDENCE_INVALID",
            "currentImagery": {"sourceId": source.id, "captureDate": source.capture_end.isoformat(), "validation": "FAILED"},
            "warnings": ["Current-imagery evidence did not satisfy the registered schema."],
        }

    failures: list[str] = []
    failures.extend(_imagery_evidence_calibration_failures(record))
    if capture_date <= lidar_reference_date:
        failures.append("IMAGERY_NOT_NEWER_THAN_LIDAR")
    if source.gsd_meters <= 0 or source.gsd_meters > 0.15:
        failures.append("IMAGERY_GSD_INSUFFICIENT")
    if context_coverage < 0.999:
        failures.append("IMAGERY_CONTEXT_COVERAGE_INCOMPLETE")
    if not quality_passed:
        failures.append("IMAGERY_QUALITY_FAILED")
    if iou < 0.95:
        failures.append("FOOTPRINT_IOU_FAILED")
    if centroid_shift > 0.75:
        failures.append("FOOTPRINT_CENTROID_SHIFTED")
    if area_change > 3:
        failures.append("FOOTPRINT_AREA_CHANGED")
    if material_change:
        failures.append("MATERIAL_STRUCTURE_CHANGE_DETECTED")

    current_imagery = {
        "sourceId": source.id,
        "captureDate": capture_date.isoformat(),
        "gsdMeters": source.gsd_meters,
        "license": source.license,
        "attribution": source.attribution,
        "modelName": str(record.get("modelName") or ""),
        "modelVersion": str(record.get("modelVersion") or ""),
        "calibrationDatasetVersion": str(record.get("calibrationDatasetVersion") or ""),
        "calibrationMetrics": record.get("calibrationMetrics") or {},
        "footprintIou": round(iou, 4),
        "centroidShiftMeters": round(centroid_shift, 3),
        "areaChangePercent": round(area_change, 3),
        "contextCoverageRatio": round(context_coverage, 4),
        "validation": "FAILED" if failures else "PASSED",
    }
    if failures:
        changed = any(
            item in failures
            for item in (
                "FOOTPRINT_IOU_FAILED",
                "FOOTPRINT_CENTROID_SHIFTED",
                "FOOTPRINT_AREA_CHANGED",
                "MATERIAL_STRUCTURE_CHANGE_DETECTED",
            )
        )
        status = "STRUCTURE_CHANGED_AFTER_LIDAR" if changed else "CURRENT_IMAGERY_INSUFFICIENT"
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": status,
            "holdReason": status,
            "currentImagery": current_imagery,
            "warnings": failures,
        }

    if lidar.age_years <= current_lidar_max_age_years:
        verification_status = "VERIFIED_CURRENT"
        pricing_allowed = True
    else:
        verification_status = "VERIFIED_HISTORICAL_UNCHANGED"
        pricing_allowed = allow_historical_verified_pricing
    return {
        "verificationStatus": verification_status,
        "pricingAllowed": pricing_allowed,
        "status": "GEOMETRY_VERIFIED" if pricing_allowed else "HISTORICAL_PRICING_CALIBRATION_HOLD",
        "holdReason": "" if pricing_allowed else "HISTORICAL_PRICING_DISABLED_DURING_CALIBRATION",
        "currentImagery": current_imagery,
        "warnings": [] if pricing_allowed else ["Historical LiDAR passed visible-change checks but pricing remains disabled."],
    }


def validate_current_structure(
    footprint: FootprintResult,
    lidar: LidarResource,
    county: str,
    registries: RegistryBundle,
    *,
    current_lidar_max_age_years: int,
    maximum_current_imagery_age_years: int,
    allow_historical_verified_pricing: bool,
    solar_reference: Any = None,
    reconstructed_geometry: dict[str, Any] | None = None,
    maximum_area_variance_percent: float = 15,
    maximum_pitch_variance_degrees: float = 10,
    provider_timeout_seconds: int = 45,
) -> dict[str, Any]:
    """Validate current structure and retain LiDAR date precision provenance."""

    result = _validate_current_structure_impl(
        footprint,
        lidar,
        county,
        registries,
        current_lidar_max_age_years=current_lidar_max_age_years,
        maximum_current_imagery_age_years=maximum_current_imagery_age_years,
        allow_historical_verified_pricing=allow_historical_verified_pricing,
        solar_reference=solar_reference,
        reconstructed_geometry=reconstructed_geometry,
        maximum_area_variance_percent=maximum_area_variance_percent,
        maximum_pitch_variance_degrees=maximum_pitch_variance_degrees,
        provider_timeout_seconds=provider_timeout_seconds,
    )
    exact_date = str(lidar.tile_acquisition_date or "").strip()
    reference_date = exact_date or str(lidar.acquired_end)
    current_imagery = dict(result.get("currentImagery") or {})
    current_imagery["lidarReferenceDate"] = reference_date
    current_imagery["lidarReferenceDatePrecision"] = (
        "EXACT_GPS_DATE" if exact_date else "REGISTERED_PROJECT_WINDOW_END"
    )
    result["currentImagery"] = current_imagery
    if not exact_date:
        warnings = list(result.get("warnings") or [])
        marker = "LIDAR_REFERENCE_USES_REGISTERED_PROJECT_WINDOW_END"
        if marker not in warnings:
            warnings.append(marker)
        result["warnings"] = warnings
    return result
