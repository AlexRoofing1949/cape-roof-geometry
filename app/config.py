"""Environment-backed production configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError("CONFIG_NUMBER_INVALID", f"{name} must be numeric.") from error


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError("CONFIG_NUMBER_INVALID", f"{name} must be an integer.") from error


@dataclass(frozen=True)
class Settings:
    bearer_token: str
    service_source_url: str
    service_commit: str
    roofer_commit: str
    roofer_version: str
    pdal_version: str
    overturemaps_version: str
    overture_release: str
    usgs_catalog_url: str
    county_boundaries_url: str
    lidar_registry_path: Path
    imagery_registry_path: Path
    work_root: Path
    minimum_longitude: float
    maximum_longitude: float
    minimum_latitude: float
    maximum_latitude: float
    footprint_search_radius_meters: float
    footprint_max_distance_meters: float
    footprint_ambiguity_meters: float
    lidar_buffer_meters: float
    maximum_lidar_age_years: int
    minimum_point_density: float
    maximum_nodata_fraction: float
    maximum_roofer_rmse_meters: float
    flat_pitch_degrees: float
    minimum_service_confidence: float
    maximum_solar_area_variance_percent: float
    maximum_solar_pitch_variance_degrees: float
    command_timeout_seconds: int
    provider_timeout_seconds: int
    catalog_cache_seconds: int
    catalog_download_timeout_seconds: int
    catalog_maximum_bytes: int
    max_concurrent_jobs: int
    minimum_lidar_coverage_ratio: float
    current_lidar_max_age_years: int
    allow_historical_verified_pricing: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        settings = cls(
            bearer_token=os.getenv("AUTH_BEARER_TOKEN", "").strip(),
            service_source_url=os.getenv("SERVICE_SOURCE_URL", "").strip(),
            service_commit=os.getenv("SERVICE_COMMIT", "").strip().lower(),
            roofer_commit=os.getenv(
                "ROOFER_COMMIT", "bb2a85a99c424001e698dac0e97485a5da31e27e"
            ).strip().lower(),
            roofer_version=os.getenv("ROOFER_VERSION", "1.0.0").strip(),
            pdal_version=os.getenv("PDAL_VERSION", "2.9.2").strip(),
            overturemaps_version=os.getenv("OVERTUREMAPS_VERSION", "1.0.1").strip(),
            overture_release=os.getenv("OVERTURE_RELEASE", "2026-08-19.0").strip(),
            usgs_catalog_url=os.getenv(
                "USGS_3DEP_CATALOG_URL", "https://usgs.entwine.io/boundaries/resources.geojson"
            ).strip(),
            county_boundaries_url=os.getenv(
                "COUNTY_BOUNDARIES_URL",
                "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query?where=STATE%3D%2712%27&outFields=NAME%2CGEOID&outSR=4326&f=geojson",
            ).strip(),
            lidar_registry_path=Path(
                os.getenv("LIDAR_REGISTRY_PATH", "/srv/cape-roof-geometry/config/lidar_sources.yaml")
            ).resolve(),
            imagery_registry_path=Path(
                os.getenv("IMAGERY_REGISTRY_PATH", "/srv/cape-roof-geometry/config/imagery_sources.yaml")
            ).resolve(),
            work_root=Path(os.getenv("WORK_ROOT", "/tmp/cape-roof-geometry")).resolve(),
            minimum_longitude=_float("SERVICE_MIN_LONGITUDE", -83.25),
            maximum_longitude=_float("SERVICE_MAX_LONGITUDE", -80.50),
            minimum_latitude=_float("SERVICE_MIN_LATITUDE", 25.50),
            maximum_latitude=_float("SERVICE_MAX_LATITUDE", 28.25),
            footprint_search_radius_meters=_float("FOOTPRINT_SEARCH_RADIUS_METERS", 45),
            footprint_max_distance_meters=_float("FOOTPRINT_MAX_DISTANCE_METERS", 20),
            footprint_ambiguity_meters=_float("FOOTPRINT_AMBIGUITY_METERS", 2),
            lidar_buffer_meters=_float("LIDAR_BUFFER_METERS", 8),
            maximum_lidar_age_years=_int("MAXIMUM_LIDAR_AGE_YEARS", 10),
            minimum_point_density=_float("MINIMUM_POINT_DENSITY", 8),
            maximum_nodata_fraction=_float("MAXIMUM_NODATA_FRACTION", 0.10),
            maximum_roofer_rmse_meters=_float("MAXIMUM_ROOFER_RMSE_METERS", 0.35),
            flat_pitch_degrees=_float("FLAT_PITCH_DEGREES", 5),
            minimum_service_confidence=_float("MINIMUM_SERVICE_CONFIDENCE", 0.80),
            maximum_solar_area_variance_percent=_float("MAXIMUM_SOLAR_AREA_VARIANCE_PERCENT", 15),
            maximum_solar_pitch_variance_degrees=_float("MAXIMUM_SOLAR_PITCH_VARIANCE_DEGREES", 10),
            command_timeout_seconds=_int("COMMAND_TIMEOUT_SECONDS", 240),
            provider_timeout_seconds=_int("PROVIDER_TIMEOUT_SECONDS", 45),
            catalog_cache_seconds=_int("CATALOG_CACHE_SECONDS", 86400),
            catalog_download_timeout_seconds=_int("CATALOG_DOWNLOAD_TIMEOUT_SECONDS", 180),
            catalog_maximum_bytes=_int("CATALOG_MAXIMUM_BYTES", 200_000_000),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
            minimum_lidar_coverage_ratio=_float("MINIMUM_LIDAR_COVERAGE_RATIO", 0.98),
            current_lidar_max_age_years=_int("CURRENT_LIDAR_MAX_AGE_YEARS", 2),
            allow_historical_verified_pricing=os.getenv(
                "ALLOW_HISTORICAL_VERIFIED_PRICING", "false"
            ).strip().lower() in {"1", "true", "yes"},
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if len(self.bearer_token) < 32:
            raise ConfigurationError("AUTH_TOKEN_MISSING", "AUTH_BEARER_TOKEN must contain at least 32 characters.")
        if not re.fullmatch(r"https://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", self.service_source_url):
            raise ConfigurationError(
                "SOURCE_URL_INVALID", "SERVICE_SOURCE_URL must identify the public GitHub repository containing this service."
            )
        for name, commit in (("SERVICE_COMMIT", self.service_commit), ("ROOFER_COMMIT", self.roofer_commit)):
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ConfigurationError("SOURCE_COMMIT_INVALID", f"{name} must be an immutable 40-character Git commit.")
        if not self.usgs_catalog_url.startswith("https://"):
            raise ConfigurationError("USGS_CATALOG_INVALID", "USGS_3DEP_CATALOG_URL must use HTTPS.")
        if not self.county_boundaries_url.startswith("https://"):
            raise ConfigurationError("COUNTY_BOUNDARIES_INVALID", "COUNTY_BOUNDARIES_URL must use HTTPS.")
        for name, path in (
            ("LIDAR_REGISTRY_PATH", self.lidar_registry_path),
            ("IMAGERY_REGISTRY_PATH", self.imagery_registry_path),
        ):
            if not path.is_file():
                raise ConfigurationError("REGISTRY_MISSING", f"{name} does not identify a readable registry file.")
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}\.\d+", self.overture_release):
            raise ConfigurationError("OVERTURE_RELEASE_INVALID", "OVERTURE_RELEASE must be a versioned release date.")
        if not (
            -180 <= self.minimum_longitude < self.maximum_longitude <= 180
            and -90 <= self.minimum_latitude < self.maximum_latitude <= 90
        ):
            raise ConfigurationError("SERVICE_BOUNDS_INVALID", "The configured service-area bounds are invalid.")
        if not 0 < self.minimum_service_confidence <= 1:
            raise ConfigurationError("CONFIDENCE_THRESHOLD_INVALID", "MINIMUM_SERVICE_CONFIDENCE must be between 0 and 1.")
        if self.max_concurrent_jobs < 1 or self.max_concurrent_jobs > 8:
            raise ConfigurationError("CONCURRENCY_INVALID", "MAX_CONCURRENT_JOBS must be between 1 and 8.")
        if not 0.98 <= self.minimum_lidar_coverage_ratio <= 1:
            raise ConfigurationError(
                "LIDAR_COVERAGE_THRESHOLD_INVALID",
                "MINIMUM_LIDAR_COVERAGE_RATIO must be between 0.98 and 1.",
            )
        if not 0 <= self.current_lidar_max_age_years <= 5:
            raise ConfigurationError(
                "CURRENT_LIDAR_AGE_INVALID", "CURRENT_LIDAR_MAX_AGE_YEARS must be between 0 and 5."
            )
        if self.catalog_maximum_bytes < 50_000_000 or self.catalog_maximum_bytes > 1_000_000_000:
            raise ConfigurationError(
                "CATALOG_SIZE_LIMIT_INVALID",
                "CATALOG_MAXIMUM_BYTES must be between 50 MB and 1 GB.",
            )
        self.work_root.mkdir(parents=True, exist_ok=True)
