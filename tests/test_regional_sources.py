import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.source_registry import load_registries

try:
    from shapely.geometry import Polygon, mapping

    from app.pipeline import _classification_histogram
    from app.providers import FootprintResult, select_regional_lidar

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
        self.assertEqual(by_id["usgs_manatee_b25_2025"].acquired_end.isoformat(), "2025-04-02")

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


if __name__ == "__main__":
    unittest.main()
