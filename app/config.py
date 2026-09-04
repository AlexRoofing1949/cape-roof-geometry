"""Environment-backed production configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
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


def _date(name: str, default: str) -> date:
    value = os.getenv(name, default).strip()
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigurationError("CONFIG_DATE_INVALID", f"{name} must use YYYY-MM-DD.") from error


@dataclass(frozen=True)
class Settings:
    bearer_token: str
    service_source_url: str
    service_commit: str
    roofer_commit: str
    roofer_version: str
    pdal_version: str
    overturemaps_version: str
    open3d_version: str
    overture_release: str
    microsoft_bfp_enabled: bool
    microsoft_bfp_release: str
    microsoft_bfp_catalog_url: str
    microsoft_bfp_zoom: int
    microsoft_bfp_maximum_tile_bytes: int
    osm_footprint_enabled: bool
    osm_overpass_url: str
    osm_cache_seconds: int
    lee_county_footprint_url: str
    solar_api_key: str
    solar_roofprint_enabled: bool
    solar_data_layer_radius_meters: float
    solar_mask_maximum_bytes: int
    solar_mask_maximum_ground_area_variance_percent: float
    solar_mask_simplification_tolerance_meters: float
    roof_edge_node_tolerance_meters: float
    roof_edge_vertical_node_tolerance_meters: float
    maximum_roofprint_perimeter_variance_percent: float
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
    footprint_consensus_min_iou: float
    footprint_correlated_min_iou: float
    footprint_maximum_centroid_separation_meters: float
    footprint_maximum_area_difference_percent: float
    footprint_review_area_difference_percent: float
    minimum_roof_hag_meters: float
    maximum_roof_hag_meters: float
    roof_cluster_tolerance_meters: float
    minimum_roof_cluster_points: int
    lidar_buffer_meters: float
    maximum_lidar_age_years: int
    minimum_lidar_acquisition_date: date
    minimum_point_density: float
    maximum_nodata_fraction: float
    maximum_roofer_rmse_meters: float
    roofer_plane_detect_epsilon_meters: float
    roofer_complexity_factor: float
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
    maximum_current_imagery_age_years: int
    allow_historical_verified_pricing: bool
    open3d_minimum_facet_points: int
    open3d_minimum_inlier_ratio: float
    open3d_maximum_assignment_distance_meters: float
    open3d_distance_threshold_meters: float
    open3d_maximum_normal_variance_degrees: float
    open3d_maximum_plane_rmse_meters: float
    open3d_ransac_iterations: int

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
            open3d_version=os.getenv("OPEN3D_VERSION", "0.19.0").strip(),
            overture_release=os.getenv("OVERTURE_RELEASE", "2026-08-19.0").strip(),
            microsoft_bfp_enabled=os.getenv("MICROSOFT_BFP_ENABLED", "true").strip().lower()
            in {"1", "true", "yes"},
            microsoft_bfp_release=os.getenv("MICROSOFT_BFP_RELEASE", "2026-07-24").strip(),
            microsoft_bfp_catalog_url=os.getenv(
                "MICROSOFT_BFP_CATALOG_URL",
                "https://bfppub.blob.core.windows.net/$web/2026-07-24/dataset-links.csv",
            ).strip(),
            microsoft_bfp_zoom=_int("MICROSOFT_BFP_ZOOM", 9),
            microsoft_bfp_maximum_tile_bytes=_int(
                "MICROSOFT_BFP_MAXIMUM_TILE_BYTES", 100_000_000
            ),
            osm_footprint_enabled=os.getenv("OSM_FOOTPRINT_ENABLED", "true").strip().lower()
            in {"1", "true", "yes"},
            osm_overpass_url=os.getenv(
                "OSM_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
            ).strip(),
            osm_cache_seconds=_int("OSM_CACHE_SECONDS", 604800),
            lee_county_footprint_url=os.getenv(
                "LEE_COUNTY_FOOTPRINT_URL",
                "https://gismapserver.leegov.com/gisserver910/rest/services/DataExplorer/LandRecords/MapServer/8/query",
            ).strip(),
            solar_api_key=os.getenv("SOLAR_API_KEY", "").strip(),
            solar_roofprint_enabled=os.getenv("SOLAR_ROOFPRINT_ENABLED", "false").strip().lower()
            in {"1", "true", "yes"},
            solar_data_layer_radius_meters=_float("SOLAR_DATA_LAYER_RADIUS_METERS", 35),
            solar_mask_maximum_bytes=_int("SOLAR_MASK_MAXIMUM_BYTES", 20_000_000),
            solar_mask_maximum_ground_area_variance_percent=_float(
                "SOLAR_MASK_MAXIMUM_GROUND_AREA_VARIANCE_PERCENT", 8
            ),
            solar_mask_simplification_tolerance_meters=_float(
                "SOLAR_MASK_SIMPLIFICATION_TOLERANCE_METERS", 0.25
            ),
            roof_edge_node_tolerance_meters=_float(
                "ROOF_EDGE_NODE_TOLERANCE_METERS", 0.10
            ),
            roof_edge_vertical_node_tolerance_meters=_float(
                "ROOF_EDGE_VERTICAL_NODE_TOLERANCE_METERS", 0.30
            ),
            maximum_roofprint_perimeter_variance_percent=_float(
                "MAXIMUM_ROOFPRINT_PERIMETER_VARIANCE_PERCENT", 10.0
            ),
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
            footprint_consensus_min_iou=_float("FOOTPRINT_CONSENSUS_MIN_IOU", 0.70),
            footprint_correlated_min_iou=_float("FOOTPRINT_CORRELATED_MIN_IOU", 0.65),
            footprint_maximum_centroid_separation_meters=_float(
                "FOOTPRINT_MAXIMUM_CENTROID_SEPARATION_METERS", 4.0
            ),
            footprint_maximum_area_difference_percent=_float(
                "FOOTPRINT_MAXIMUM_AREA_DIFFERENCE_PERCENT", 16.0
            ),
            footprint_review_area_difference_percent=_float(
                "FOOTPRINT_REVIEW_AREA_DIFFERENCE_PERCENT", 20.0
            ),
            minimum_roof_hag_meters=_float("MINIMUM_ROOF_HAG_METERS", 1.5),
            maximum_roof_hag_meters=_float("MAXIMUM_ROOF_HAG_METERS", 25.0),
            roof_cluster_tolerance_meters=_float("ROOF_CLUSTER_TOLERANCE_METERS", 1.5),
            minimum_roof_cluster_points=_int("MINIMUM_ROOF_CLUSTER_POINTS", 20),
            lidar_buffer_meters=_float("LIDAR_BUFFER_METERS", 8),
            maximum_lidar_age_years=_int("MAXIMUM_LIDAR_AGE_YEARS", 10),
            minimum_lidar_acquisition_date=_date(
                "MINIMUM_LIDAR_ACQUISITION_DATE", "2018-01-01"
            ),
            minimum_point_density=_float("MINIMUM_POINT_DENSITY", 8),
            maximum_nodata_fraction=_float("MAXIMUM_NODATA_FRACTION", 0.10),
            maximum_roofer_rmse_meters=_float("MAXIMUM_ROOFER_RMSE_METERS", 0.35),
            roofer_plane_detect_epsilon_meters=_float(
                "ROOFER_PLANE_DETECT_EPSILON_METERS", 0.15
            ),
            roofer_complexity_factor=_float("ROOFER_COMPLEXITY_FACTOR", 0.95),
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
            maximum_current_imagery_age_years=_int("MAXIMUM_CURRENT_IMAGERY_AGE_YEARS", 2),
            allow_historical_verified_pricing=os.getenv(
                "ALLOW_HISTORICAL_VERIFIED_PRICING", "false"
            ).strip().lower() in {"1", "true", "yes"},
            open3d_minimum_facet_points=_int("OPEN3D_MINIMUM_FACET_POINTS", 20),
            open3d_minimum_inlier_ratio=_float("OPEN3D_MINIMUM_INLIER_RATIO", 0.65),
            open3d_maximum_assignment_distance_meters=_float(
                "OPEN3D_MAXIMUM_ASSIGNMENT_DISTANCE_METERS", 0.60
            ),
            open3d_distance_threshold_meters=_float("OPEN3D_DISTANCE_THRESHOLD_METERS", 0.15),
            open3d_maximum_normal_variance_degrees=_float(
                "OPEN3D_MAXIMUM_NORMAL_VARIANCE_DEGREES", 5
            ),
            open3d_maximum_plane_rmse_meters=_float("OPEN3D_MAXIMUM_PLANE_RMSE_METERS", 0.15),
            open3d_ransac_iterations=_int("OPEN3D_RANSAC_ITERATIONS", 1000),
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
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", self.microsoft_bfp_release):
            raise ConfigurationError(
                "MICROSOFT_BFP_RELEASE_INVALID", "MICROSOFT_BFP_RELEASE must be an immutable release date."
            )
        for name, url in (
            ("MICROSOFT_BFP_CATALOG_URL", self.microsoft_bfp_catalog_url),
            ("OSM_OVERPASS_URL", self.osm_overpass_url),
            ("LEE_COUNTY_FOOTPRINT_URL", self.lee_county_footprint_url),
        ):
            if not url.startswith("https://"):
                raise ConfigurationError("FOOTPRINT_PROVIDER_URL_INVALID", f"{name} must use HTTPS.")
        if self.solar_roofprint_enabled and len(self.solar_api_key) < 20:
            raise ConfigurationError(
                "SOLAR_API_KEY_MISSING",
                "SOLAR_API_KEY must be configured when the Google Solar roof-mask provider is enabled.",
            )
        if not (
            10 <= self.solar_data_layer_radius_meters <= 100
            and 1_000_000 <= self.solar_mask_maximum_bytes <= 50_000_000
            and 1 <= self.solar_mask_maximum_ground_area_variance_percent <= 10
            and 0.10 <= self.solar_mask_simplification_tolerance_meters <= 0.50
            and 0.02 <= self.roof_edge_node_tolerance_meters <= 0.15
            and 0.10 <= self.roof_edge_vertical_node_tolerance_meters <= 0.40
        ):
            raise ConfigurationError(
                "SOLAR_ROOFPRINT_CONFIG_INVALID",
                "Google Solar roof-mask limits are outside the supported fail-closed range.",
            )
        if not 2 <= self.maximum_roofprint_perimeter_variance_percent <= 15:
            raise ConfigurationError(
                "ROOFPRINT_PERIMETER_THRESHOLD_INVALID",
                "MAXIMUM_ROOFPRINT_PERIMETER_VARIANCE_PERCENT must be between 2 and 15.",
            )
        if self.microsoft_bfp_zoom != 9 or self.microsoft_bfp_maximum_tile_bytes < 25_000_000:
            raise ConfigurationError(
                "MICROSOFT_BFP_CONFIG_INVALID",
                "Microsoft footprint tiles must use the published zoom and a safe download limit.",
            )
        if not 0.70 <= self.footprint_consensus_min_iou <= 0.95:
            raise ConfigurationError(
                "FOOTPRINT_CONSENSUS_INVALID",
                "FOOTPRINT_CONSENSUS_MIN_IOU must be between 0.70 and 0.95.",
            )
        if not 0.60 <= self.footprint_correlated_min_iou < self.footprint_consensus_min_iou:
            raise ConfigurationError(
                "FOOTPRINT_CORRELATION_INVALID",
                "The correlated-source threshold must be below the independent consensus threshold.",
            )
        if not (
            0 < self.footprint_maximum_centroid_separation_meters <= 5
            and 0 < self.footprint_maximum_area_difference_percent <= 16
            and self.footprint_maximum_area_difference_percent
            < self.footprint_review_area_difference_percent
            <= 25
        ):
            raise ConfigurationError(
                "FOOTPRINT_COMPARISON_INVALID", "The footprint agreement thresholds are unsafe."
            )
        if not 3600 <= self.osm_cache_seconds <= 2_592_000:
            raise ConfigurationError(
                "OSM_CACHE_INVALID", "OSM_CACHE_SECONDS must be between one hour and 30 days."
            )
        if not 0.5 <= self.minimum_roof_hag_meters < self.maximum_roof_hag_meters <= 40:
            raise ConfigurationError(
                "ROOF_HAG_RANGE_INVALID",
                "The class-1 roof height-above-ground range is outside the supported bounds.",
            )
        if not 0.25 <= self.roof_cluster_tolerance_meters <= 3 or self.minimum_roof_cluster_points < 10:
            raise ConfigurationError(
                "ROOF_CLUSTER_CONFIG_INVALID", "The roof-point clustering thresholds are unsafe."
            )
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
        if not 0.05 <= self.roofer_plane_detect_epsilon_meters <= 0.30:
            raise ConfigurationError(
                "ROOFER_PLANE_EPSILON_INVALID",
                "ROOFER_PLANE_DETECT_EPSILON_METERS must be between 0.05 and 0.30.",
            )
        if not 0.50 <= self.roofer_complexity_factor <= 1.0:
            raise ConfigurationError(
                "ROOFER_COMPLEXITY_FACTOR_INVALID",
                "ROOFER_COMPLEXITY_FACTOR must be between 0.50 and 1.0.",
            )
        if not date(2018, 1, 1) <= self.minimum_lidar_acquisition_date <= date.today():
            raise ConfigurationError(
                "LIDAR_MINIMUM_DATE_INVALID",
                "MINIMUM_LIDAR_ACQUISITION_DATE must be between 2018-01-01 and today.",
            )
        if not 0 <= self.current_lidar_max_age_years <= 5:
            raise ConfigurationError(
                "CURRENT_LIDAR_AGE_INVALID", "CURRENT_LIDAR_MAX_AGE_YEARS must be between 0 and 5."
            )
        if not 0 <= self.maximum_current_imagery_age_years <= 5:
            raise ConfigurationError(
                "CURRENT_IMAGERY_AGE_INVALID",
                "MAXIMUM_CURRENT_IMAGERY_AGE_YEARS must be between 0 and 5.",
            )
        if self.catalog_maximum_bytes < 50_000_000 or self.catalog_maximum_bytes > 1_000_000_000:
            raise ConfigurationError(
                "CATALOG_SIZE_LIMIT_INVALID",
                "CATALOG_MAXIMUM_BYTES must be between 50 MB and 1 GB.",
            )
        if not 0.5 <= self.open3d_minimum_inlier_ratio <= 1:
            raise ConfigurationError(
                "OPEN3D_INLIER_THRESHOLD_INVALID",
                "OPEN3D_MINIMUM_INLIER_RATIO must be between 0.5 and 1.",
            )
        if self.open3d_minimum_facet_points < 10 or self.open3d_ransac_iterations < 100:
            raise ConfigurationError(
                "OPEN3D_SUPPORT_THRESHOLD_INVALID",
                "Open3D support and RANSAC thresholds are below the safe minimum.",
            )
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.open3d_version):
            raise ConfigurationError(
                "OPEN3D_VERSION_INVALID", "OPEN3D_VERSION must be a pinned semantic version."
            )
        if not (
            0 < self.open3d_maximum_assignment_distance_meters <= 1.0
            and self.open3d_distance_threshold_meters
            < self.open3d_maximum_assignment_distance_meters
            and 0 < self.open3d_distance_threshold_meters <= 0.30
            and 0 < self.open3d_maximum_plane_rmse_meters <= 0.30
            and 0 < self.open3d_maximum_normal_variance_degrees <= 15
        ):
            raise ConfigurationError(
                "OPEN3D_GEOMETRY_THRESHOLD_INVALID",
                "Open3D geometry thresholds are outside the supported fail-closed range.",
            )
        self.work_root.mkdir(parents=True, exist_ok=True)
