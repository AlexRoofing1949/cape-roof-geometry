import math
import unittest
from pathlib import Path

from app.cityjson_geometry import extract_roof_geometry, load_cityjson_feature


FIXTURES = Path(__file__).parent / "fixtures"


class CityJsonGeometryTests(unittest.TestCase):
    def test_simple_gable_uses_actual_3d_edges(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        result = extract_roof_geometry(feature, transform)

        horizontal_area_square_meters = 60
        expected_area = (
            horizontal_area_square_meters / math.cos(math.radians(45))
        ) * 10.763910416709722
        self.assertAlmostEqual(result["roofAreaSqFt"], expected_area, delta=0.02)
        self.assertAlmostEqual(result["averagePitchDegrees"], 45, delta=0.01)
        self.assertAlmostEqual(result["maximumPitchDegrees"], 45, delta=0.01)
        self.assertAlmostEqual(result["eavesFeet"], 20 * 3.280839895013123, delta=0.02)
        self.assertAlmostEqual(result["rakesFeet"], 4 * math.sqrt(18) * 3.280839895013123, delta=0.03)
        self.assertEqual(result["valleysFeet"], 0)
        self.assertAlmostEqual(result["ridgesFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertEqual(len(result["facets"]), 2)
        self.assertEqual(len(result["rakes"]), 4)
        self.assertEqual(len(result["eaves"]), 2)
        self.assertEqual(result["valleys"], [])

    def test_low_density_fails_closed(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        feature["CityObjects"]["TEST-GABLE"]["attributes"]["rf_pt_density"] = 2
        with self.assertRaisesRegex(Exception, "point density") as context:
            extract_roof_geometry(feature, transform)
        self.assertEqual(context.exception.code, "LIDAR_DENSITY_TOO_LOW")

    def test_roofer_multisurface_semantics_are_measured(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        geometry = feature["CityObjects"]["TEST-GABLE-0"]["geometry"][0]
        geometry["type"] = "MultiSurface"
        geometry["boundaries"] = geometry["boundaries"][0]
        geometry["semantics"]["values"] = geometry["semantics"]["values"][0]

        result = extract_roof_geometry(feature, transform)

        self.assertEqual(len(result["facets"]), 2)
        self.assertAlmostEqual(result["averagePitchDegrees"], 45, delta=0.01)
        self.assertGreater(result["roofAreaSqFt"], 0)

    def test_missing_facets_return_sanitized_schema_audit(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        geometry = feature["CityObjects"]["TEST-GABLE-0"]["geometry"][0]
        for surface in geometry["semantics"]["surfaces"]:
            if surface["type"] == "RoofSurface":
                surface["type"] = "GenericSurface"

        with self.assertRaisesRegex(Exception, "did not reconstruct") as context:
            extract_roof_geometry(feature, transform)

        summary = context.exception.details["cityJsonGeometrySummary"]
        self.assertEqual(summary[0]["geometryType"], "Solid")
        self.assertEqual(summary[0]["boundaryDepth"], 4)
        self.assertNotIn("vertices", context.exception.details)

    def test_missing_transform_is_allowed_for_unquantized_fixture(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        self.assertIsNone(transform)
        result = extract_roof_geometry(feature, transform)
        self.assertGreater(result["roofAreaSqFt"], 0)


if __name__ == "__main__":
    unittest.main()
