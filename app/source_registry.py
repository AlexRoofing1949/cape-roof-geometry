"""Validated, versioned registries for regional LiDAR and current imagery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError


SERVICE_COUNTIES = frozenset(
    {"Manatee", "Sarasota", "DeSoto", "Charlotte", "Lee", "Collier", "Glades", "Hendry"}
)
PENDING_LICENSE_MARKERS = frozenset({"", "PENDING", "PENDING_AGENCY_CONFIRMATION", "UNKNOWN"})
ALLOWED_ACCESS_TYPES = frozenset({"ept", "usgs_tnm", "local_ept"})


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigurationError("REGISTRY_INVALID", f"Unable to read registry {path.name}.") from error
    if not isinstance(payload, dict):
        raise ConfigurationError("REGISTRY_INVALID", f"Registry {path.name} must contain an object.")
    return payload


def _iso_date(value: Any, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as error:
        raise ConfigurationError("REGISTRY_DATE_INVALID", f"{field} must use YYYY-MM-DD.") from error
    if parsed.year < 2000 or parsed > datetime.now(timezone.utc).date():
        raise ConfigurationError("REGISTRY_DATE_INVALID", f"{field} is outside the supported date range.")
    return parsed


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not str(item).strip() for item in value):
        raise ConfigurationError("REGISTRY_FIELD_INVALID", f"{field} must contain at least one value.")
    return tuple(str(item).strip() for item in value)


def _int_list(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError("REGISTRY_FIELD_INVALID", f"{field} must contain point classes.")
    try:
        result = tuple(sorted({int(item) for item in value}))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("REGISTRY_FIELD_INVALID", f"{field} must contain integers.") from error
    if any(item < 0 or item > 255 for item in result):
        raise ConfigurationError("REGISTRY_FIELD_INVALID", f"{field} contains an invalid LAS class.")
    return result


@dataclass(frozen=True)
class LidarSource:
    id: str
    dataset_name: str
    priority: int
    counties: tuple[str, ...]
    acquired_start: date
    acquired_end: date
    access: str
    endpoint: str | None
    catalog_name_patterns: tuple[str, ...]
    data_kind: str
    minimum_density_ppsm: float
    license: str
    enabled: bool
    allowed_classes: tuple[int, ...]
    roof_classes: tuple[int, ...]
    provider: str
    metadata_url: str
    tile_index_file: Path | None
    tile_date_field: str
    tile_endpoint_field: str

    @property
    def license_verified(self) -> bool:
        return self.license.upper() not in PENDING_LICENSE_MARKERS

    @property
    def acquisition_date(self) -> date:
        return self.acquired_end

    @property
    def age_years(self) -> int:
        today = datetime.now(timezone.utc).date()
        return max(0, today.year - self.acquisition_date.year - ((today.month, today.day) < (self.acquisition_date.month, self.acquisition_date.day)))


@dataclass(frozen=True)
class ImagerySource:
    id: str
    counties: tuple[str, ...]
    capture_start: date
    capture_end: date
    gsd_meters: float
    license: str
    commercial_estimate_use_allowed: bool
    enabled: bool
    evidence_file: Path | None
    evidence_kind: str
    evidence_endpoint: str
    imagery_endpoint: str
    attribution: str


@dataclass(frozen=True)
class RegistryBundle:
    schema_version: str
    version_hash: str
    lidar_sources: tuple[LidarSource, ...]
    imagery_sources: tuple[ImagerySource, ...]


def load_registries(lidar_path: Path, imagery_path: Path) -> RegistryBundle:
    lidar_payload = _read_yaml(lidar_path)
    imagery_payload = _read_yaml(imagery_path)
    if str(lidar_payload.get("schema_version")) != "1.0" or str(imagery_payload.get("schema_version")) != "1.0":
        raise ConfigurationError("REGISTRY_SCHEMA_UNSUPPORTED", "Source registries must use schema version 1.0.")

    lidar_sources: list[LidarSource] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(lidar_payload.get("sources") or []):
        if not isinstance(raw, dict):
            raise ConfigurationError("REGISTRY_SOURCE_INVALID", "Each LiDAR source must be an object.")
        source_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_]{5,80}", source_id) or source_id in seen_ids:
            raise ConfigurationError("REGISTRY_SOURCE_ID_INVALID", f"Invalid or duplicate LiDAR source ID at index {index}.")
        seen_ids.add(source_id)
        counties = _string_list(raw.get("counties"), f"{source_id}.counties")
        if not set(counties).issubset(SERVICE_COUNTIES):
            raise ConfigurationError("REGISTRY_COUNTY_INVALID", f"{source_id} contains a county outside the service territory.")
        access = str(raw.get("access") or "").strip()
        if access not in ALLOWED_ACCESS_TYPES:
            raise ConfigurationError("REGISTRY_ACCESS_INVALID", f"{source_id}.access is unsupported.")
        endpoint = str(raw.get("endpoint") or "").strip() or None
        if endpoint and not endpoint.startswith("https://") and access != "local_ept":
            raise ConfigurationError("REGISTRY_ENDPOINT_INVALID", f"{source_id}.endpoint must use HTTPS.")
        acquired_start = _iso_date(raw.get("acquired_start"), f"{source_id}.acquired_start")
        acquired_end = _iso_date(raw.get("acquired_end"), f"{source_id}.acquired_end")
        if acquired_end < acquired_start:
            raise ConfigurationError("REGISTRY_DATE_INVALID", f"{source_id} acquisition dates are reversed.")
        allowed_classes = _int_list(raw.get("allowed_classes"), f"{source_id}.allowed_classes")
        roof_classes = _int_list(raw.get("roof_classes"), f"{source_id}.roof_classes")
        if not set(roof_classes).issubset(set(allowed_classes)):
            raise ConfigurationError("REGISTRY_CLASS_INVALID", f"{source_id}.roof_classes must be allowed classes.")
        minimum_density = float(raw.get("minimum_density_ppsm") or 0)
        if minimum_density <= 0:
            raise ConfigurationError("REGISTRY_DENSITY_INVALID", f"{source_id} requires a positive minimum density.")
        patterns = tuple(str(item) for item in (raw.get("catalog_name_patterns") or []))
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ConfigurationError("REGISTRY_PATTERN_INVALID", f"{source_id} has an invalid catalog pattern.") from error
        if access == "usgs_tnm" and not patterns:
            raise ConfigurationError("REGISTRY_PATTERN_MISSING", f"{source_id} requires catalog_name_patterns.")
        if access in {"ept", "local_ept"} and not endpoint and bool(raw.get("enabled")):
            raise ConfigurationError("REGISTRY_ENDPOINT_MISSING", f"Enabled source {source_id} requires an endpoint.")
        lidar_sources.append(
            LidarSource(
                id=source_id,
                dataset_name=str(raw.get("dataset_name") or source_id).strip(),
                priority=int(raw.get("priority") or 0),
                counties=counties,
                acquired_start=acquired_start,
                acquired_end=acquired_end,
                access=access,
                endpoint=endpoint,
                catalog_name_patterns=patterns,
                data_kind=str(raw.get("data_kind") or "").strip(),
                minimum_density_ppsm=minimum_density,
                license=str(raw.get("license") or "").strip(),
                enabled=bool(raw.get("enabled")),
                allowed_classes=allowed_classes,
                roof_classes=roof_classes,
                provider=str(raw.get("provider") or "").strip(),
                metadata_url=str(raw.get("metadata_url") or "").strip(),
                tile_index_file=(lidar_path.parent / str(raw.get("tile_index_file"))).resolve()
                if raw.get("tile_index_file")
                else None,
                tile_date_field=str(raw.get("tile_date_field") or "acquisition_date").strip(),
                tile_endpoint_field=str(raw.get("tile_endpoint_field") or "endpoint").strip(),
            )
        )

    imagery_sources: list[ImagerySource] = []
    for index, raw in enumerate(imagery_payload.get("sources") or []):
        if not isinstance(raw, dict):
            raise ConfigurationError("REGISTRY_SOURCE_INVALID", "Each imagery source must be an object.")
        source_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_]{5,80}", source_id):
            raise ConfigurationError("REGISTRY_SOURCE_ID_INVALID", f"Invalid imagery source ID at index {index}.")
        counties = _string_list(raw.get("counties"), f"{source_id}.counties")
        if not set(counties).issubset(SERVICE_COUNTIES):
            raise ConfigurationError("REGISTRY_COUNTY_INVALID", f"{source_id} contains an unsupported county.")
        evidence_text = str(raw.get("evidence_file") or "").strip()
        evidence_file = (imagery_path.parent / evidence_text).resolve() if evidence_text else None
        evidence_endpoint = str(raw.get("evidence_endpoint") or "").strip()
        imagery_endpoint = str(raw.get("imagery_endpoint") or "").strip()
        if (evidence_endpoint and not evidence_endpoint.startswith("https://")) or (
            imagery_endpoint and not imagery_endpoint.startswith("https://")
        ):
            raise ConfigurationError(
                "REGISTRY_ENDPOINT_INVALID", f"{source_id} imagery evidence endpoints must use HTTPS."
            )
        imagery_sources.append(
            ImagerySource(
                id=source_id,
                counties=counties,
                capture_start=_iso_date(raw.get("capture_start"), f"{source_id}.capture_start"),
                capture_end=_iso_date(raw.get("capture_end"), f"{source_id}.capture_end"),
                gsd_meters=float(raw.get("gsd_meters") or 0),
                license=str(raw.get("license") or "").strip(),
                commercial_estimate_use_allowed=bool(raw.get("commercial_estimate_use_allowed")),
                enabled=bool(raw.get("enabled")),
                evidence_file=evidence_file,
                evidence_kind=str(raw.get("evidence_kind") or "immutable_file").strip(),
                evidence_endpoint=evidence_endpoint,
                imagery_endpoint=imagery_endpoint,
                attribution=str(raw.get("attribution") or "").strip(),
            )
        )

    canonical = json.dumps(
        {"lidar": lidar_payload, "imagery": imagery_payload}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return RegistryBundle(
        schema_version="1.0",
        version_hash=hashlib.sha256(canonical).hexdigest(),
        lidar_sources=tuple(lidar_sources),
        imagery_sources=tuple(imagery_sources),
    )
