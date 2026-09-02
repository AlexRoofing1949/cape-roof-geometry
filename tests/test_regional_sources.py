import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.source_registry import load_registries

try:
    from shapely.geometry import Polygon, mapping

    from app.pipeline import _classification_histogram, _exact_gps_acquisition_date
    from app.errors import UnreliableGeometryError
    from app.providers import (
        FootprintResult,
        fetch_overture_footprint,
        resolve_service_county,
        select_regional_lidar,
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
        self.assertIn("usgs_florida_peninsular_2018_2020", [item.source_id for item in candidates])

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
