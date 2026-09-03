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
