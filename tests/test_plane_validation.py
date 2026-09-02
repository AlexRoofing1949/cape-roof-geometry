import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app.plane_validation import _open3d, validate_facet_points
from app.pipeline import runtime_dependencies


def settings():
    return SimpleNamespace(
        open3d_minimum_facet_points=20,
        open3d_minimum_inlier_ratio=0.65,
        open3d_distance_threshold_meters=0.05,
        open3d_maximum_normal_variance_degrees=5,
        open3d_maximum_plane_rmse_meters=0.05,
        open3d_ransac_iterations=500,
    )


class PlaneValidationTests(unittest.TestCase):
    def test_open3d_native_library_failure_fails_closed(self):
        with patch(
            "app.plane_validation.importlib.import_module",
            side_effect=ImportError("native dependency unavailable"),
        ):
            with self.assertRaisesRegex(Exception, "validator is unavailable") as raised:
                _open3d()
        self.assertEqual(raised.exception.code, "OPEN3D_RUNTIME_MISSING")
        self.assertEqual(raised.exception.http_status, 422)
        self.assertFalse(raised.exception.retryable)

    def test_health_detects_unloadable_open3d_runtime(self):
        with (
            patch("app.pipeline.shutil.which", return_value="/usr/bin/dependency"),
            patch(
                "app.pipeline.importlib.import_module",
                side_effect=ImportError("native dependency unavailable"),
            ),
        ):
            dependencies = runtime_dependencies()
        self.assertFalse(dependencies["open3d"])
        self.assertTrue(all(dependencies[name] for name in ("roofer", "pdal", "ogr2ogr", "overturemaps")))

    def test_matching_plane_passes(self):
        points = np.asarray(
            [[x, y, 3 + 0.5 * y] for x in np.linspace(0.1, 9.9, 12) for y in np.linspace(0.1, 4.9, 8)],
            dtype=float,
        )
        normal = [0, -0.5, 1]
        result = validate_facet_points(
            points,
            [
                {
                    "facetId": "F1",
                    "verticesMeters": [[0, 0, 3], [10, 0, 3], [10, 5, 5.5], [0, 5, 5.5]],
                    "normal": normal,
                }
            ],
            settings(),
        )
        self.assertEqual(result["validation"], "PASSED")
        self.assertEqual(result["facetCount"], 1)
        self.assertAlmostEqual(result["facets"][0]["normalVarianceDegrees"], 0, delta=0.01)

    def test_disagreeing_plane_fails_closed(self):
        points = np.asarray(
            [[x, y, 3 + 0.5 * y] for x in np.linspace(0.1, 9.9, 12) for y in np.linspace(0.1, 4.9, 8)],
            dtype=float,
        )
        with self.assertRaisesRegex(Exception, "disagrees") as raised:
            validate_facet_points(
                points,
                [
                    {
                        "facetId": "F1",
                        "verticesMeters": [[0, 0, 3], [10, 0, 3], [10, 5, 5.5], [0, 5, 5.5]],
                        "normal": [0, 0, 1],
                    }
                ],
                settings(),
            )
        self.assertEqual(raised.exception.code, "OPEN3D_PLANE_VALIDATION_FAILED")
        self.assertIn("PLANE_NORMAL_DISAGREEMENT", raised.exception.details["failures"])


if __name__ == "__main__":
    unittest.main()
