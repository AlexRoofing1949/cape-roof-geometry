"""Fail-closed validation of registered, commercially authorized current imagery evidence."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from shapely.geometry import shape

from .providers import FootprintResult, LidarResource, transform_geometry, utm_epsg
from .source_registry import PENDING_LICENSE_MARKERS, RegistryBundle


def _distance_and_change(reference, current) -> tuple[float, float, float]:
    longitude = float(reference.centroid.x)
    latitude = float(reference.centroid.y)
    epsg = utm_epsg(longitude, latitude)
    reference_m = transform_geometry(reference, "EPSG:4326", f"EPSG:{epsg}")
    current_m = transform_geometry(current, "EPSG:4326", f"EPSG:{epsg}")
    union = reference_m.union(current_m).area
    iou = reference_m.intersection(current_m).area / max(union, 0.01)
    shift = reference_m.centroid.distance(current_m.centroid)
    area_change = abs(current_m.area - reference_m.area) / max(reference_m.area, 0.01) * 100
    return float(iou), float(shift), float(area_change)


def validate_current_structure(
    footprint: FootprintResult,
    lidar: LidarResource,
    county: str,
    registries: RegistryBundle,
    *,
    current_lidar_max_age_years: int,
    allow_historical_verified_pricing: bool,
) -> dict[str, Any]:
    """Validate immutable evidence created from an authorized orthophoto workflow.

    The service never scrapes map viewers. An imagery source becomes usable only
    after its registry entry is enabled and its evidence file is packaged with
    the immutable service version.
    """

    lidar_reference_date = date.fromisoformat(lidar.tile_acquisition_date or lidar.acquired_end)
    if not lidar.tile_acquisition_date:
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "LIDAR_TILE_DATE_UNAVAILABLE",
            "holdReason": "LIDAR_TILE_DATE_UNAVAILABLE",
            "currentImagery": {"sourceId": "", "captureDate": "", "validation": "NOT_RUN"},
            "warnings": ["The selected project has an acquisition window but no exact registered tile date."],
        }

    eligible = [
        source
        for source in registries.imagery_sources
        if source.enabled
        and county in source.counties
        and source.commercial_estimate_use_allowed
        and source.license.upper() not in PENDING_LICENSE_MARKERS
        and source.capture_end > lidar_reference_date
        and source.evidence_file is not None
        and source.evidence_file.is_file()
    ]
    eligible.sort(key=lambda source: source.capture_end, reverse=True)
    if not eligible:
        return {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "CURRENT_IMAGERY_UNAVAILABLE",
            "holdReason": "CURRENT_IMAGERY_UNAVAILABLE",
            "currentImagery": {"sourceId": "", "captureDate": "", "validation": "NOT_RUN"},
            "warnings": ["No enabled, authorized current-imagery evidence covers this county."],
        }

    source = eligible[0]
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
