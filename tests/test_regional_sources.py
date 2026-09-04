import json
import tempfile
import unittest
from io import BytesIO
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.errors import ConfigurationError
from app.source_registry import load_registries

try:
    from shapely.geometry import Polygon, mapping

    from app.pipeline import (
        _classification_histogram,
        _enforce_lidar_acquisition_floor,
        _exact_gps_acquisition_date,
        _pdal_crop,
    )
    from app.errors import NoCoverageError, UnreliableGeometryError
    from app.providers import (
        FootprintResult,
        _bing_quadkey,
        _simplify_google_solar_mask,
        fetch_best_footprint,
        fetch_google_solar_roofprint,
        fetch_overture_footprint,
        resolve_service_county,
        select_regional_lidar,
        transform_geometry,
        utm_epsg,
    )

    SPATIAL_RUNTIME_AVAILABLE = True
except ModuleNotFoundError:
    SPATIAL_RUNTIME_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


def footprint():
    geometry = Polygon(
        [(-81.9510, 26.6210), (-81.9508, 26.6210), (-81.9508, 26.6212), (-81.9510, 26.6212)]
    )
    return FootprintResult(geometry, "building-1", "2026-08-19.0", 0.2, [])


def settings():
    return SimpleNamespace(
        lidar_buffer_meters=8,
        minimum_lidar_coverage_ratio=0.98,
        maximum_lidar_age_years=10,
        minimum_lidar_acquisition_date=date(2018, 1, 1),
    )


class RegionalSourceTests(unittest.TestCase):
    def setUp(self):
        self.registries = load_registries(
            ROOT / "config" / "lidar_sources.yaml", ROOT / "config" / "imagery_sources.yaml"
        )

    def test_registry_keeps_lee_2026_disabled_and_noaa_surface_returns(self):
        by_id = {source.id: source for source in self.registries.lidar_sources}
        self.assertFalse(by_id["lcmcd_lee_2026"].enabled)
        self.assertEqual(by_id["lcmcd_lee_2026"].license, "PENDING_AGENCY_CONFIRMATION")
        self.assertIn(1, by_id["noaa_pre_ian_2022"].allowed_classes)
        self.assertIn(1, by_id["noaa_post_ian_2022"].roof_classes)
        self.assertIn(1, by_id["usgs_florida_peninsular_2018_2020"].allowed_classes)
        self.assertIn(1, by_id["usgs_florida_peninsular_2018_2020"].roof_classes)
        self.assertEqual(by_id["usgs_manatee_b25_2025"].acquired_end.isoformat(), "2025-04-02")

    def test_registry_rejects_non_lee_eagle_view_source(self):
        imagery = """\
schema_version: "1.0"
sources:
  - id: manatee_eagle_view
    counties: [Manatee]
    capture_start: "2026-01-01"
    capture_end: "2026-02-01"
    gsd_meters: 0.0762
    license: PUBLIC
    commercial_estimate_use_allowed: true
    enabled: true
    evidence_endpoint: https://example.invalid/footprint
    imagery_endpoint: https://example.invalid/eagleview
    attribution: EagleView
"""
        with tempfile.TemporaryDirectory() as directory:
            imagery_path = Path(directory) / "imagery_sources.yaml"
            imagery_path.write_text(imagery, encoding="utf-8")
            with self.assertRaises(ConfigurationError) as raised:
                load_registries(ROOT / "config" / "lidar_sources.yaml", imagery_path)
        self.assertEqual(raised.exception.code, "REGISTRY_PROVIDER_NOT_ALLOWED")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_microsoft_quadkey_is_stable_for_cape_coral(self):
        self.assertEqual(_bing_quadkey(-81.9495, 26.5629, 9), "032023011")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.fetch_osm_footprint")
    @patch("app.providers.fetch_county_footprint")
    @patch("app.providers.fetch_microsoft_footprint")
    @patch("app.providers.fetch_overture_footprint")
    def test_footprint_cascade_retains_microsoft_after_county_consensus(
        self, overture, microsoft, county, osm
    ):
        geometry = footprint().geometry_wgs84
        overture.side_effect = NoCoverageError("BUILDING_FOOTPRINT_NOT_FOUND", "missing")
        microsoft.return_value = FootprintResult(
            geometry,
            "ms-1",
            "2026-07-24",
            0.0,
            [],
            "Microsoft GlobalML Building Footprints",
            "CDLA-Permissive-2.0",
            "Microsoft GlobalML Building Footprints",
        )
        county.return_value = FootprintResult(
            geometry.buffer(0.000001),
            "lee-1",
            "2026-03-22",
            0.0,
            [],
            "Lee County Building Footprints",
            "LEE-COUNTY-PUBLIC-GIS",
            "Lee County Property Appraiser and Lee County GIS",
            lineage_group="COUNTY_AUTHORITATIVE",
        )
        configured = SimpleNamespace(
            footprint_consensus_min_iou=0.70,
            footprint_correlated_min_iou=0.65,
            footprint_maximum_centroid_separation_meters=4,
            footprint_maximum_area_difference_percent=16,
            footprint_review_area_difference_percent=20,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_best_footprint(-81.9509, 26.6211, Path(directory), configured)
        self.assertEqual(result.provider, "Microsoft GlobalML Building Footprints")
        self.assertEqual(result.consensus_status, "CORROBORATED")
        self.assertGreaterEqual(result.consensus_records[-2]["intersectionOverUnion"], 0.70)
        self.assertIn("boundaryHausdorffDistanceMeters", result.consensus_records[-2])
        self.assertEqual(
            result.consensus_records[-1]["decision"],
            "PRIMARY_GEOMETRY_RETAINED_AFTER_CORROBORATION",
        )
        self.assertEqual(
            result.consensus_records[-1]["corroboratedBy"],
            "Lee County Building Footprints",
        )
        osm.assert_not_called()

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.fetch_county_footprint")
    @patch("app.providers.fetch_overture_footprint")
    def test_footprint_cascade_retains_overture_after_county_consensus(self, overture, county):
        geometry = footprint().geometry_wgs84
        overture.return_value = FootprintResult(
            geometry,
            "overture-1",
            "2026-08-19.0",
            0.0,
            [],
        )
        county.return_value = FootprintResult(
            geometry.buffer(0.000001),
            "lee-1",
            "2026-03-22",
            0.0,
            [],
            "Lee County Building Footprints",
            "LEE-COUNTY-PUBLIC-GIS",
            "Lee County Property Appraiser and Lee County GIS",
            lineage_group="COUNTY_AUTHORITATIVE",
        )
        configured = SimpleNamespace(
            footprint_consensus_min_iou=0.70,
            footprint_correlated_min_iou=0.65,
            footprint_maximum_centroid_separation_meters=4,
            footprint_maximum_area_difference_percent=16,
            footprint_review_area_difference_percent=20,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_best_footprint(-81.9509, 26.6211, Path(directory), configured)
        self.assertEqual(result.provider, "Overture Maps Buildings")
        self.assertEqual(result.consensus_status, "CORROBORATED")
        self.assertEqual(
            result.consensus_records[-1]["decision"],
            "PRIMARY_GEOMETRY_RETAINED_AFTER_CORROBORATION",
        )
        self.assertEqual(
            result.consensus_records[-1]["corroboratedBy"],
            "Lee County Building Footprints",
        )

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._polygonize_google_solar_mask")
    @patch("app.providers._download_file")
    @patch("app.providers.urllib.request.urlopen")
    def test_google_solar_roofprint_requires_mask_ground_area_and_building_agreement(
        self, urlopen, download, polygonize
    ):
        building = footprint()
        mask = building.geometry_wgs84.buffer(-0.000005)
        polygonize.return_value = [mask]
        urlopen.return_value = BytesIO(
            json.dumps(
                {
                    "imageryDate": {"year": 2019, "month": 6, "day": 5},
                    "imageryQuality": "HIGH",
                    "maskUrl": "https://solar.googleapis.com/v1/geoTiff:get?id=test",
                }
            ).encode("utf-8")
        )
        projected = transform_geometry(
            mask,
            "EPSG:4326",
            f"EPSG:{utm_epsg(-81.9509, 26.6211)}",
        )
        ground_area_sqft = projected.area * 10.763910416709722
        solar = SimpleNamespace(
            facets=[
                SimpleNamespace(
                    areaSqFt=ground_area_sqft / 0.9,
                    groundAreaSqFt=ground_area_sqft,
                    pitchDegrees=25,
                )
            ]
        )
        configured = SimpleNamespace(
            solar_roofprint_enabled=True,
            solar_api_key="test-key-with-safe-minimum-length",
            solar_data_layer_radius_meters=35,
            solar_mask_maximum_bytes=20_000_000,
            solar_mask_maximum_ground_area_variance_percent=5,
            solar_mask_simplification_tolerance_meters=0.25,
            provider_timeout_seconds=30,
            footprint_max_distance_meters=20,
            footprint_ambiguity_meters=2,
            footprint_correlated_min_iou=0.65,
            footprint_maximum_centroid_separation_meters=4,
            footprint_review_area_difference_percent=20,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_google_solar_roofprint(
                building,
                -81.9509,
                26.6211,
                Path(directory),
                configured,
                solar,
            )
        self.assertEqual(result.provider, "Google Solar Building Mask")
        self.assertEqual(result.consensus_status, "ROOF_MASK_CORROBORATED")
        self.assertEqual(
            result.consensus_records[-1]["decision"],
            "ROOFTOP_MASK_SELECTED_FOR_RECONSTRUCTION",
        )
        self.assertLess(result.consensus_records[-1]["groundAreaVariancePercent"], 0.01)
        self.assertIn("maskSimplification", result.consensus_records[-1])
        download.assert_called_once()

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_google_solar_mask_stair_steps_are_safely_simplified(self):
        stair_step = Polygon(
            [
                (0, 0),
                (10, 0),
                (10, 10),
                (8, 10.1),
                (6, 9.9),
                (4, 10.1),
                (2, 9.9),
                (0, 10),
            ]
        )

        simplified, audit = _simplify_google_solar_mask(stair_step, 0.25)

        self.assertTrue(simplified.is_valid)
        self.assertLess(audit["simplifiedVertexCount"], audit["rawVertexCount"])
        self.assertLessEqual(audit["areaChangePercent"], 2.0)
        self.assertEqual(audit["toleranceMeters"], 0.25)

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_google_solar_mask_reduces_tolerance_when_area_would_change(self):
        notched = Polygon(
            [
                (0, 0),
                (10, 0),
                (10, 10),
                (8, 10),
                (8, 9.6),
                (2, 9.6),
                (2, 10),
                (0, 10),
            ]
        )

        simplified, audit = _simplify_google_solar_mask(notched, 0.50)

        self.assertAlmostEqual(simplified.area, notched.area, places=6)
        self.assertEqual(audit["requestedToleranceMeters"], 0.50)
        self.assertEqual(audit["toleranceMeters"], 0.25)
        self.assertTrue(audit["fallbackApplied"])
        self.assertGreater(audit["attempts"][0]["areaChangePercent"], 2.0)

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.fetch_county_footprint")
    @patch("app.providers.fetch_overture_footprint")
    def test_footprint_consensus_conflict_fails_closed(self, overture, county):
        first = footprint().geometry_wgs84
        second = Polygon(
            [(-81.9501, 26.6210), (-81.9499, 26.6210), (-81.9499, 26.6212), (-81.9501, 26.6212)]
        )
        overture.return_value = FootprintResult(first, "overture-1", "2026-08-19.0", 0, [])
        county.return_value = FootprintResult(
            second,
            "lee-2",
            "2026-03-22",
            0,
            [],
            "Lee County Building Footprints",
            "LEE-COUNTY-PUBLIC-GIS",
            "Lee County Property Appraiser and Lee County GIS",
            lineage_group="COUNTY_AUTHORITATIVE",
        )
        configured = SimpleNamespace(
            footprint_consensus_min_iou=0.70,
            footprint_correlated_min_iou=0.65,
            footprint_maximum_centroid_separation_meters=4,
            footprint_maximum_area_difference_percent=16,
            footprint_review_area_difference_percent=20,
        )
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            UnreliableGeometryError
        ) as raised:
            fetch_best_footprint(-81.9509, 26.6211, Path(directory), configured)
        self.assertEqual(raised.exception.code, "FOOTPRINT_PROVIDER_CONFLICT")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.fetch_osm_footprint")
    @patch("app.providers.fetch_county_footprint")
    @patch("app.providers.fetch_overture_footprint")
    def test_osm_does_not_independently_corroborate_overture(self, overture, county, osm):
        geometry = footprint().geometry_wgs84
        overture.return_value = FootprintResult(geometry, "overture-1", "2026-08-19.0", 0, [])
        county.side_effect = NoCoverageError("COUNTY_FOOTPRINT_UNAVAILABLE", "missing")
        osm.return_value = FootprintResult(
            geometry,
            "way/1",
            "live-overpass",
            0,
            [],
            "OpenStreetMap Buildings",
            "ODbL-1.0",
            "OpenStreetMap contributors",
        )
        configured = SimpleNamespace(
            footprint_consensus_min_iou=0.70,
            footprint_correlated_min_iou=0.65,
            footprint_maximum_centroid_separation_meters=4,
            footprint_maximum_area_difference_percent=16,
            footprint_review_area_difference_percent=20,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_best_footprint(-81.9509, 26.6211, Path(directory), configured)
        self.assertEqual(result.consensus_status, "CORRELATED_SUPPORT_ONLY")
        self.assertFalse(result.consensus_records[-1]["independent"])

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._require_pinned_overture_release", side_effect=["2026-08-19.0", "2026-08-19.0"])
    @patch("app.providers._run")
    def test_overture_download_works_around_absolute_catalog_link_bug(self, run, _release):
        observed = {}

        def write_fixture(command, *, timeout):
            observed["command"] = command
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "id": "overture-building-1",
                                "properties": {"sources": []},
                                "geometry": mapping(
                                    Polygon(
                                        [
                                            (-81.9588, 26.6324),
                                            (-81.9583, 26.6324),
                                            (-81.9583, 26.6329),
                                            (-81.9588, 26.6329),
                                            (-81.9588, 26.6324),
                                        ]
                                    )
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

        run.side_effect = write_fixture
        configured = SimpleNamespace(
            overture_release="2026-08-19.0",
            footprint_search_radius_meters=45,
            footprint_max_distance_meters=20,
            footprint_ambiguity_meters=2,
            provider_timeout_seconds=45,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_overture_footprint(
                -81.95855, 26.63265, Path(directory), configured
            )
        self.assertEqual(result.overture_release, "2026-08-19.0")
        self.assertFalse(any(value.startswith("--release") for value in observed["command"]))

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._current_overture_release", return_value="2026-09-16.0")
    def test_overture_release_mismatch_fails_closed(self, _release):
        from app.providers import _require_pinned_overture_release

        configured = SimpleNamespace(overture_release="2026-08-19.0")
        with self.assertRaises(UnreliableGeometryError) as raised:
            _require_pinned_overture_release(configured)
        self.assertEqual(raised.exception.code, "OVERTURE_RELEASE_MISMATCH")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._cached_json_url")
    def test_tigerweb_county_suffix_is_normalized(self, cached_json):
        cached_json.return_value = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"NAME": "Lee County", "GEOID": "12071"},
                    "geometry": mapping(
                        Polygon([(-82.2, 26.4), (-81.7, 26.4), (-81.7, 26.9), (-82.2, 26.9)])
                    ),
                }
            ],
        }
        configured = SimpleNamespace(
            county_boundaries_url="https://example.invalid/florida-counties",
            catalog_cache_seconds=86400,
            catalog_download_timeout_seconds=180,
            catalog_maximum_bytes=200_000_000,
            work_root=ROOT,
        )
        self.assertEqual(resolve_service_county(footprint().geometry_wgs84, configured), "Lee")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._catalog_features", return_value=iter(()))
    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._ept_coverage")
    def test_lee_ranks_post_ian_before_pre_ian(self, ept_coverage, _county, _catalog):
        ept_coverage.return_value = (Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)]), 1_000_000)
        county, candidates, audit = select_regional_lidar(
            footprint(), -81.9509, 26.6211, settings(), self.registries
        )
        self.assertEqual(county, "Lee")
        self.assertEqual(candidates[0].source_id, "noaa_post_ian_2022")
        self.assertEqual(candidates[1].source_id, "noaa_pre_ian_2022")
        self.assertTrue(all(item.source_id != "lcmcd_lee_2026" for item in candidates))
        self.assertTrue(any(item["sourceId"] == "lcmcd_lee_2026" for item in audit))

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._catalog_features", return_value=iter(()))
    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._ept_coverage")
    def test_registry_source_before_2018_floor_is_rejected(
        self, ept_coverage, _county, _catalog
    ):
        ept_coverage.return_value = (
            Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)]),
            1_000_000,
        )
        old_fallback = replace(
            next(
                source
                for source in self.registries.lidar_sources
                if source.id == "usgs_southwest_2018_2019"
            ),
            acquired_start=date(2017, 1, 1),
            acquired_end=date(2017, 12, 31),
        )
        registries = replace(
            self.registries,
            lidar_sources=tuple(
                old_fallback
                if source.id == "usgs_southwest_2018_2019"
                else source
                for source in self.registries.lidar_sources
            ),
        )

        _, candidates, audit = select_regional_lidar(
            footprint(), -81.9509, 26.6211, settings(), registries
        )

        self.assertNotIn(old_fallback.id, [item.source_id for item in candidates])
        self.assertTrue(
            any(
                item["sourceId"] == old_fallback.id
                and item["decision"] == "REJECTED_BEFORE_MINIMUM_ACQUISITION_DATE"
                and item["minimumAcquisitionDate"] == "2018-01-01"
                for item in audit
            )
        )

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_exact_property_crop_before_2018_floor_is_rejected(self):
        lidar = SimpleNamespace(source_id="test-pre-2018")
        with self.assertRaises(UnreliableGeometryError) as raised:
            _enforce_lidar_acquisition_floor(lidar, "2017-12-31", settings())
        self.assertEqual(
            raised.exception.code, "LIDAR_TILE_BEFORE_MINIMUM_ACQUISITION_DATE"
        )

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_exact_property_crop_on_2018_floor_is_allowed(self):
        lidar = SimpleNamespace(source_id="test-2018")
        self.assertEqual(
            _enforce_lidar_acquisition_floor(lidar, "2018-01-01", settings()),
            date(2018, 1, 1),
        )

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers._catalog_features", return_value=iter(()))
    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._ept_coverage")
    def test_lee_orders_by_acquisition_date_before_coverage(
        self, ept_coverage, _county, _catalog
    ):
        broad = Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)])
        tighter = Polygon([(-82.01, 26.55), (-81.89, 26.55), (-81.89, 26.70), (-82.01, 26.70)])
        ept_coverage.side_effect = [(tighter, 1_000_000), (broad, 1_000_000)]
        _, candidates, _ = select_regional_lidar(
            footprint(), -81.9509, 26.6211, settings(), self.registries
        )
        self.assertEqual(candidates[0].source_id, "noaa_post_ian_2022")
        self.assertEqual(candidates[1].source_id, "noaa_pre_ian_2022")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._catalog_features")
    @patch("app.providers._ept_coverage")
    def test_lee_includes_exact_southwest_2018_catalog_project(
        self, ept_coverage, catalog, _county
    ):
        ept_coverage.return_value = (
            Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)]),
            1_000_000,
        )
        catalog.return_value = iter(
            [
                {
                    "type": "Feature",
                    "geometry": mapping(Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)])),
                    "properties": {
                        "name": "USGS_LPC_FL_Southwest_A_2018_LAS_2019",
                        "url": "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_FL_Southwest_A_2018_LAS_2019/ept.json",
                        "count": 500000,
                    },
                }
            ]
        )
        _, candidates, _ = select_regional_lidar(
            footprint(), -81.9509, 26.6211, settings(), self.registries
        )
        self.assertIn("usgs_southwest_2018_2019", [item.source_id for item in candidates])

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.resolve_service_county", return_value="Collier")
    @patch("app.providers._catalog_features")
    @patch("app.providers._ept_coverage")
    def test_collier_includes_southwest_b_2018_catalog_project(
        self, ept_coverage, catalog, _county
    ):
        ept_coverage.return_value = (
            Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)]),
            1_000_000,
        )
        catalog.return_value = iter(
            [
                {
                    "type": "Feature",
                    "geometry": mapping(
                        Polygon([(-83, 25), (-80, 25), (-80, 28), (-83, 28)])
                    ),
                    "properties": {
                        "name": "USGS_LPC_FL_Southwest_B_2018_LAS_2019",
                        "url": "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_FL_Southwest_B_2018_LAS_2019/ept.json",
                        "count": 500000,
                    },
                }
            ]
        )

        county, candidates, audit = select_regional_lidar(
            footprint(), -81.7020683, 26.2587423, settings(), self.registries
        )

        self.assertEqual(county, "Collier")
        candidate = next(
            item for item in candidates if item.source_id == "usgs_southwest_2018_2019"
        )
        self.assertEqual(candidate.acquired_start, "2018-05-08")
        self.assertEqual(candidate.acquired_end, "2019-03-01")
        self.assertTrue(
            any(
                item["sourceId"] == "usgs_southwest_2018_2019"
                and item["decision"] == "CANDIDATE"
                for item in audit
            )
        )

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.providers.resolve_service_county", return_value="Manatee")
    @patch("app.providers._catalog_features")
    def test_manatee_uses_registered_2025_project_date(self, catalog, _county):
        catalog.return_value = iter(
            [
                {
                    "type": "Feature",
                    "geometry": mapping(Polygon([(-83, 25), (-80, 25), (-80, 29), (-83, 29)])),
                    "properties": {
                        "name": "USGS_LPC_FL_Manatee_2025",
                        "url": "https://usgs-lidar-public.s3.amazonaws.com/FL_Manatee_2025/ept.json",
                        "count": 500000,
                    },
                }
            ]
        )
        county, candidates, _ = select_regional_lidar(
            footprint(), -81.9509, 26.6211, settings(), self.registries
        )
        self.assertEqual(county, "Manatee")
        self.assertEqual(candidates[0].source_id, "usgs_manatee_b25_2025")
        self.assertEqual(candidates[0].acquired_end, "2025-04-02")
        self.assertEqual(candidates[0].tile_acquisition_date, "")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_classification_histogram_is_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "filters.stats": {
                                "statistic": [
                                    {
                                        "name": "Classification",
                                        "counts": [
                                            {"value": 1, "count": 120},
                                            {"value": 2, "count": 80},
                                            {"value": 6, "count": 40},
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_classification_histogram(path), {"1": 120, "2": 80, "6": 40})

    def test_classification_histogram_accepts_pdal_count_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "filters.stats": {
                                "statistic": [
                                    {
                                        "name": "Classification",
                                        "counts": ["1.000000/120", "2.000000/80", "6.000000/40"],
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_classification_histogram(path), {"1": 120, "2": 80, "6": 40})

    def test_empty_classification_statistic_is_auditable_no_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "filters.stats": {
                                "statistic": [
                                    {
                                        "name": "Classification",
                                        "count": 0,
                                        "bins": {},
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_classification_histogram(path), {})

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    @patch("app.pipeline._run")
    def test_unclassified_roof_returns_are_normalized_after_audit(self, run):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            output = workspace / "roof.laz"
            output.write_bytes(b"0" * 2048)
            (workspace / "pdal-metadata.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "filters.stats": {
                                "statistic": [
                                    {
                                        "name": "Classification",
                                        "counts": ["1.000000/120", "2.000000/80"],
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            lidar = SimpleNamespace(
                ept_url="https://example.com/ept.json",
                allowed_classes=(1, 2, 6),
                roof_classes=(1, 6),
                source_id="usgs_florida_peninsular_2018_2020",
                acquired_start="2018-01-01",
                acquired_end="2020-12-31",
            )

            audit = _pdal_crop(
                lidar,
                "POLYGON((-82 26,-82 27,-81 27,-81 26,-82 26))",
                32617,
                output,
                workspace,
                SimpleNamespace(
                    command_timeout_seconds=30,
                    minimum_roof_hag_meters=1.5,
                    maximum_roof_hag_meters=25,
                    roof_cluster_tolerance_meters=1.5,
                    minimum_roof_cluster_points=20,
                ),
            )

            stages = audit["pipeline"]["pipeline"]
            stage_types = [stage["type"] for stage in stages]
            self.assertLess(stage_types.index("filters.stats"), stage_types.index("filters.assign"))
            self.assertLess(stage_types.index("filters.outlier"), stage_types.index("filters.stats"))
            self.assertEqual(stage_types.count("filters.mortonorder"), 1)
            self.assertLess(
                stage_types.index("filters.reprojection"),
                stage_types.index("filters.mortonorder"),
            )
            self.assertLess(
                stage_types.index("filters.mortonorder"),
                stage_types.index("filters.outlier"),
            )
            self.assertLess(stage_types.index("filters.reprojection"), stage_types.index("filters.hag_delaunay"))
            self.assertLess(stage_types.index("filters.hag_delaunay"), stage_types.index("filters.cluster"))
            self.assertLess(stage_types.index("filters.cluster"), stage_types.index("filters.normal"))
            self.assertEqual(
                sum(
                    stage.get("expression") == "ClusterID > 0"
                    for stage in stages
                    if stage.get("type") == "filters.expression"
                ),
                1,
            )
            self.assertLess(stage_types.index("filters.cluster"), stage_types.index("filters.assign"))
            self.assertEqual(
                audit["rooferClassNormalization"],
                ["Classification = 6 WHERE Classification == 1"],
            )
            self.assertEqual(audit["classHistogram"], {"1": 120, "2": 80})
            self.assertEqual(audit["noiseFilter"]["outlierClassRemoved"], 7)
            self.assertEqual(
                audit["pointOrder"],
                {
                    "provider": "PDAL filters.mortonorder",
                    "method": "XY Morton order",
                    "reverse": False,
                },
            )
            self.assertTrue(audit["classOneCorrection"]["applied"])
            self.assertEqual(audit["classOneCorrection"]["normalKnn"], 12)
            self.assertEqual(audit["classOneCorrection"]["minimumNormalZ"], 0.65)
            self.assertEqual(audit["classOneCorrection"]["maximumCurvature"], 0.12)
            self.assertIn(
                "Classification == 1 && NormalZ >= 0.65 && Curvature <= 0.12",
                next(
                    stage["expression"]
                    for stage in stages
                    if stage.get("type") == "filters.expression"
                    and "NormalZ" in stage.get("expression", "")
                ),
            )
            run.assert_called_once()

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "container spatial dependencies are not installed")
    def test_exact_gps_date_is_derived_only_inside_registered_window(self):
        epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)
        acquired = datetime(2022, 11, 17, 12, 0, tzinfo=timezone.utc)
        adjusted_gps_seconds = (acquired - epoch).total_seconds() - 1_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "filters.stats": {
                                "statistic": [
                                    {
                                        "name": "GpsTime",
                                        "minimum": adjusted_gps_seconds,
                                        "maximum": adjusted_gps_seconds + 3600,
                                        "average": adjusted_gps_seconds + 1800,
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            lidar = SimpleNamespace(acquired_start="2022-11-16", acquired_end="2022-11-18")
            self.assertEqual(_exact_gps_acquisition_date(path, lidar), "2022-11-17")


if __name__ == "__main__":
    unittest.main()
