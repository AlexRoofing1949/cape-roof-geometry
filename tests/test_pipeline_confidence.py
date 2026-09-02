import unittest
from types import SimpleNamespace

from app.pipeline import _combined_confidence


class PipelineConfidenceTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return SimpleNamespace(maximum_lidar_age_years=10, footprint_max_distance_meters=20)

    def test_historical_lidar_keeps_age_penalty_without_current_validation(self):
        settings = self._settings()
        score, components = _combined_confidence(
            0.80,
            0,
            8,
            0.80,
            settings,
            current_structure_validated=False,
        )

        self.assertEqual(components["temporalVerification"], components["lidarAge"])
        self.assertLess(score, 0.80)

    def test_passed_current_structure_validation_satisfies_temporal_component(self):
        settings = self._settings()
        score, components = _combined_confidence(
            0.80,
            0,
            8,
            0.80,
            settings,
            current_structure_validated=True,
        )

        self.assertLess(components["lidarAge"], 1.0)
        self.assertEqual(components["temporalVerification"], 1.0)
        self.assertGreaterEqual(score, 0.80)


if __name__ == "__main__":
    unittest.main()
