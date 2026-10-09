from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "eagleview_calibration.py"
SPEC = importlib.util.spec_from_file_location("eagleview_calibration", MODULE_PATH)
assert SPEC and SPEC.loader
calibration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calibration
SPEC.loader.exec_module(calibration)


class EagleViewCalibrationTests(unittest.TestCase):
    @staticmethod
    def _candidate(topology_hash: str = "a" * 64):
        pitch = math.degrees(math.atan(0.5))
        return {
            "geometry": {
                "roofAreaSqFt": 100.0,
                "averagePitchDegrees": pitch,
                "facets": [
                    {
                        "areaSqFt": 100.0,
                        "horizontalAreaSqFt": 100.0 * math.cos(math.radians(pitch)),
                        "pitchDegrees": pitch,
                    }
                ],
                "ridgesFeet": 10.0,
                "hipsFeet": 20.0,
                "valleysFeet": 5.0,
                "rakesFeet": 12.0,
                "eavesFeet": 30.0,
                "topology": {"topologyHash": topology_hash},
            }
        }

    @staticmethod
    def _reference():
        return calibration.ReferenceMeasurements(
            report_id="73026931",
            roof_area_sq_ft=100.0,
            facet_count=1,
            predominant_pitch_rise=6.0,
            ridges_ft=10.0,
            hips_ft=20.0,
            valleys_ft=5.0,
            rakes_ft=12.0,
            eaves_ft=30.0,
        )

    def test_candidate_requires_edges_and_repeatable_topology(self):
        candidate = self._candidate()
        result = calibration.evaluate_candidate_runs(
            self._reference(), [candidate, candidate]
        )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["topologyHashDeterministic"])
        self.assertFalse(result["inspectionRequired"])

    def test_candidate_edge_error_is_a_mandatory_gate(self):
        first = self._candidate()
        second = self._candidate()
        first["geometry"]["hipsFeet"] = 22.0
        result = calibration.evaluate_candidate_runs(
            self._reference(), [first, second], maximum_edge_error_feet=1.0
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("HIPS_ERROR_EXCEEDED", result["runs"][0]["failures"])
        self.assertTrue(result["inspectionRequired"])

    def test_candidate_topology_hash_must_repeat_exactly(self):
        result = calibration.evaluate_candidate_runs(
            self._reference(), [self._candidate("a" * 64), self._candidate("b" * 64)]
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["topologyHashDeterministic"])
        self.assertIn("TOPOLOGY_HASH_NOT_DETERMINISTIC", result["runs"][0]["failures"])

    def test_parse_summary_and_edges(self):
        result = calibration.parse_eagleview_text(
            """
            Report: 73026931
            Total Roof Area =3,571 sq ft
            Total Roof Facets =9
            Predominant Pitch =6/12
            Ridges = 59 ft
            Hips = 127 ft
            Valleys = 44 ft
            Rakes = 35 ft
            Eaves = 231 ft
            Flashing = 2 ft
            Step flashing = 17 ft
            """
        )
        self.assertEqual(result.report_id, "73026931")
        self.assertEqual(result.roof_area_sq_ft, 3571)
        self.assertEqual(result.facet_count, 9)
        self.assertAlmostEqual(result.predominant_pitch_degrees, math.degrees(math.atan(0.5)))
        self.assertEqual(result.ridges_ft, 59)
        self.assertEqual(result.step_flashing_ft, 17)

    def test_hip_length_does_not_match_cover_page_ridge_hip_aggregate(self):
        result = calibration.parse_eagleview_text(
            """
            Report: 28464089
            Total Roof Area =7,895 sq ft
            Total Roof Facets =44
            Predominant Pitch =6/12
            Total Ridges/Hips =626 ft
            Ridges = 106 ft
            Hips = 520 ft
            Valleys = 200 ft
            Rakes = 3 ft
            Eaves/Starter = 646 ft
            """
        )
        self.assertEqual(result.ridges_ft, 106)
        self.assertEqual(result.hips_ft, 520)
        self.assertEqual(result.eaves_ft, 646)

    def test_obj_area_pitch_facets_and_cosine_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "123456.obj"
            path.write_text(
                "\n".join(
                    [
                        "v 0 0 0",
                        "v 10 0 5",
                        "v 10 10 5",
                        "v 0 10 0",
                        "o Roof.A",
                        "f 1 2 3",
                        "f 1 3 4",
                        "o Roof.A.Label",
                        "f 1 1 1",
                    ]
                ),
                encoding="utf-8",
            )
            result = calibration.parse_eagleview_obj(path)
        self.assertAlmostEqual(result.horizontal_area_sq_ft, 100.0, places=6)
        self.assertAlmostEqual(result.roof_area_sq_ft, 100 * math.sqrt(1.25), places=6)
        self.assertAlmostEqual(result.area_weighted_pitch_degrees, math.degrees(math.atan(0.5)), places=6)
        self.assertEqual(result.predominant_pitch_rise, 6.0)
        self.assertAlmostEqual(result.predominant_pitch_degrees, math.degrees(math.atan(0.5)), places=6)
        self.assertEqual(result.facet_count, 1)
        self.assertEqual(result.triangle_count, 2)
        self.assertLess(result.maximum_formula_error_percent, 1e-9)


if __name__ == "__main__":
    unittest.main()
