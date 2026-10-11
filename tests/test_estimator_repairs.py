"""Regression checks for the October 11 repair; no customer data or network."""
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from shapely.geometry import box, mapping
from app.errors import NoCoverageError, TransientProviderError, UnreliableGeometryError
from app.pipeline import _roofprint_point_density
from app.providers import FootprintResult, _select_footprint_feature, select_regional_lidar
from app.source_registry import load_registries

ROOT = Path(__file__).resolve().parents[1]

class EstimatorRepairTests(unittest.TestCase):
    def setUp(self):
        self.outline = box(-81.9510, 26.6210, -81.9508, 26.6212)
        self.footprint = FootprintResult(self.outline, "synthetic", "2026-08-19.0", 0, [])
        self.registries = load_registries(ROOT / "config/lidar_sources.yaml", ROOT / "config/imagery_sources.yaml")
        self.settings = SimpleNamespace(lidar_buffer_meters=8, minimum_lidar_coverage_ratio=0.98,
            maximum_lidar_age_years=10, minimum_lidar_acquisition_date=date(2018, 1, 1))

    def select(self):
        return select_regional_lidar(self.footprint, -81.9509, 26.6211, self.settings, self.registries)

    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._catalog_features", return_value=[])
    def test_one_manifest_outage_does_not_hide_valid_fallback(self, _catalog, _county):
        calls = []
        def coverage(url, _settings):
            calls.append(url)
            if len(calls) == 1:
                raise TransientProviderError("TEST_TIMEOUT", "Synthetic timeout")
            return box(-83, 25, -80, 28), 1000000
        with patch("app.providers._ept_coverage", side_effect=coverage):
            county, candidates, audit = self.select()
        self.assertEqual(county, "Lee")
        self.assertTrue(candidates)
        self.assertTrue(any(row.get("errorCode") == "TEST_TIMEOUT" for row in audit))
        self.assertNotIn(calls[0], [item.ept_url for item in candidates])

    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._catalog_features", side_effect=TransientProviderError("CATALOG_TIMEOUT", "Test"))
    @patch("app.providers._ept_coverage", return_value=(box(-83, 25, -80, 28), 1000000))
    def test_catalog_outage_does_not_hide_direct_noaa(self, _ept, catalog, _county):
        _, candidates, audit = self.select()
        self.assertEqual(candidates[0].source_id, "noaa_post_ian_2022")
        self.assertTrue(any(row.get("errorCode") == "CATALOG_TIMEOUT" for row in audit))
        catalog.assert_called_once()

    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._catalog_features", return_value=[])
    @patch("app.providers._ept_coverage", side_effect=TransientProviderError("TEST_TIMEOUT", "Test"))
    def test_all_manifest_outages_are_retryable_not_no_coverage(self, _ept, _catalog, _county):
        with self.assertRaises(TransientProviderError) as raised:
            self.select()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "LIDAR_SOURCES_TEMPORARILY_UNAVAILABLE")

    @patch("app.providers.resolve_service_county", return_value="Lee")
    @patch("app.providers._catalog_features", return_value=[])
    @patch("app.providers._ept_coverage", return_value=(box(0, 0, 1, 1), 100))
    def test_true_no_coverage_is_not_reported_as_retryable(self, _ept, _catalog, _county):
        with self.assertRaises(NoCoverageError):
            self.select()

    def select_polygons(self, polygons, longitude=-81.9509):
        features = [{"type": "Feature", "geometry": mapping(polygon),
                     "properties": {"id": str(i)}} for i, polygon in enumerate(polygons)]
        return _select_footprint_feature(features, longitude, 26.6211,
            SimpleNamespace(footprint_max_distance_meters=20, footprint_ambiguity_meters=2),
            provider="synthetic", release="test", license_name="test", attribution="test")

    def test_duplicate_outlines_are_not_two_buildings(self):
        result = self.select_polygons([self.outline, self.outline])
        self.assertTrue(result.geometry_wgs84.equals(self.outline))

    def test_strict_containment_resolves_close_neighbor(self):
        neighbor = box(-81.950795, 26.6210, -81.950595, 26.6212)
        result = self.select_polygons([self.outline, neighbor], longitude=-81.950805)
        self.assertTrue(result.geometry_wgs84.equals(self.outline))

    def test_distinct_overlapping_buildings_still_require_review(self):
        with self.assertRaises(UnreliableGeometryError) as raised:
            self.select_polygons([self.outline, self.outline.buffer(0.00001)])
        self.assertEqual(raised.exception.code, "BUILDING_FOOTPRINT_AMBIGUOUS")

    def test_density_counts_roof_inside_unbuffered_outline_only(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            footprint = box(0, 0, 20, 10)
            def run(command, **kwargs):
                pipeline = json.loads(Path(command[-1]).read_text())["pipeline"]
                self.assertEqual(pipeline[1]["polygon"], footprint.wkt)
                self.assertEqual(pipeline[2]["expression"], "Classification == 6")
                Path(command[command.index("--metadata") + 1]).write_text(json.dumps({
                    "statistic": [{"name": "Classification", "count": 2000, "counts": ["6.000000/2000"]}]}))
            with patch("app.pipeline._run", side_effect=run):
                result = _roofprint_point_density(workspace / "roof.laz", footprint,
                             workspace, SimpleNamespace(command_timeout_seconds=5))
        self.assertEqual(result["roofprintReturnCount"], 2000)
        self.assertEqual(result["pointDensityPpsm"], 10)

    def test_invalid_density_denominator_rejected_before_pdal(self):
        for area in (0, -1, float("nan"), float("inf")):
            with self.subTest(area=area), patch("app.pipeline._run") as run:
                with self.assertRaises(UnreliableGeometryError):
                    _roofprint_point_density(Path("unused"), SimpleNamespace(area=area), Path("unused"), SimpleNamespace())
                run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
