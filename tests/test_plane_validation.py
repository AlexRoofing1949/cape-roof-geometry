import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app.plane_validation import _open3d, validate_facet_points, validate_roofer_planes
from app.pipeline import _run_roofer, runtime_dependencies


def settings():
    return SimpleNamespace(
        open3d_minimum_facet_points=20,
        open3d_minimum_inlier_ratio=0.65,
        open3d_maximum_assignment_distance_meters=0.6,
        open3d_distance_threshold_meters=0.05,
        open3d_maximum_normal_variance_degrees=5,
        open3d_maximum_plane_rmse_meters=0.05,
        open3d_ransac_iterations=500,
    )


class PlaneValidationTests(unittest.TestCase):
    def test_roofer_uses_production_plane_detection_tolerance(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pointcloud = workspace / "roof.laz"
            footprint = workspace / "roof.gpkg"
            output = workspace / "roofer-output"
            pointcloud.write_bytes(b"test")
            footprint.write_bytes(b"test")

            def fake_run(command, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                (output / "roof.city.jsonl").write_text("{}\n", encoding="utf-8")
                self.assertEqual(
                    command[command.index("--plane-detect-epsilon") + 1], "0.15"
                )
                self.assertEqual(
                    command[command.index("--complexity-factor") + 1], "0.95"
                )

            configured = SimpleNamespace(
                command_timeout_seconds=30,
                roofer_plane_detect_epsilon_meters=0.15,
                roofer_complexity_factor=0.95,
            )
            with patch("app.pipeline._run", side_effect=fake_run):
                feature, metadata = _run_roofer(
                    pointcloud, footprint, output, configured
                )

            self.assertEqual(feature, output / "roof.city.jsonl")
            self.assertIsNone(metadata)

    def test_roofer_validator_exports_all_normalized_roof_returns_at_high_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pointcloud = workspace / "roof.laz"
            pointcloud.write_bytes(b"test")
            runtime = SimpleNamespace(__version__="0.19.0")
            configured = SimpleNamespace(
                open3d_version="0.19.0",
                command_timeout_seconds=30,
            )
            with (
                patch("app.plane_validation._open3d", return_value=runtime),
                patch("app.plane_validation.subprocess.run"),
                patch(
                    "app.plane_validation.np.loadtxt",
                    return_value=np.asarray([[0, 0, 0], [1, 1, 1]], dtype=float),
                ),
                patch(
                    "app.plane_validation.validate_facet_points",
                    return_value={"validation": "PASSED"},
                ),
            ):
                result = validate_roofer_planes(pointcloud, [], workspace, configured)

            pipeline = json.loads(
                (workspace / "open3d-points-pipeline.json").read_text(encoding="utf-8")
            )["pipeline"]
            writer = pipeline[-1]
            stage_types = [stage["type"] if isinstance(stage, dict) else "reader" for stage in pipeline]
            self.assertEqual(result["validation"], "PASSED")
            self.assertEqual(stage_types, ["reader", "filters.expression", "writers.text"])
            self.assertEqual(pipeline[1]["expression"], "Classification == 6")
            self.assertEqual(writer["type"], "writers.text")
            self.assertEqual(writer["order"], "X:8,Y:8,Z:8")
            self.assertFalse(writer["keep_unspecified"])

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
        self.assertEqual(result["facets"][0]["pointAxisSpanMeters"]["x"], 9.8)
        self.assertEqual(result["facets"][0]["pointAxisSpanMeters"]["y"], 4.8)
        self.assertEqual(len(result["facets"][0]["pointCloudSingularValues"]), 3)

    def test_large_projected_coordinates_are_centered_before_plane_fit(self):
        east = 412_345.0
        north = 2_938_765.0
        points = np.asarray(
            [
                [east + x, north + y, 3 + 0.5 * y]
                for x in np.linspace(0.1, 9.9, 12)
                for y in np.linspace(0.1, 4.9, 8)
            ],
            dtype=float,
        )
        result = validate_facet_points(
            points,
            [
                {
                    "facetId": "F1",
                    "verticesMeters": [
                        [east, north, 3],
                        [east + 10, north, 3],
                        [east + 10, north + 5, 5.5],
                        [east, north + 5, 5.5],
                    ],
                    "normal": [0, -0.5, 1],
                }
            ],
            settings(),
        )

        self.assertEqual(result["validation"], "PASSED")
        self.assertAlmostEqual(result["facets"][0]["normalVarianceDegrees"], 0, delta=0.01)

    def test_overlapping_plan_view_facets_use_nearest_3d_plane(self):
        lower = np.asarray(
            [[x, y, 3 + 0.5 * y] for x in np.linspace(0.1, 9.9, 12) for y in np.linspace(0.1, 4.9, 8)],
            dtype=float,
        )
        upper = np.asarray(
            [[x, y, 8 - 0.5 * y] for x in np.linspace(0.1, 9.9, 12) for y in np.linspace(0.1, 4.9, 8)],
            dtype=float,
        )
        facets = [
            {
                "facetId": "F1",
                "verticesMeters": [[0, 0, 3], [10, 0, 3], [10, 5, 5.5], [0, 5, 5.5]],
                "normal": [0, -0.5, 1],
            },
            {
                "facetId": "F2",
                "verticesMeters": [[0, 0, 8], [0, 5, 5.5], [10, 5, 5.5], [10, 0, 8]],
                "normal": [0, 0.5, 1],
            },
        ]

        result = validate_facet_points(np.vstack([lower, upper]), facets, settings())

        self.assertEqual(result["validation"], "PASSED")
        self.assertEqual(result["facetCount"], 2)
        self.assertEqual(result["facets"][0]["planViewCandidatePoints"], 192)
        self.assertEqual(result["facets"][0]["supportPoints"], 96)
        self.assertEqual(result["facets"][1]["supportPoints"], 96)

    def test_plane_assignment_discards_returns_far_from_every_reconstructed_plane(self):
        roof = np.asarray(
            [[x, y, 3 + 0.5 * y] for x in np.linspace(0.1, 9.9, 12) for y in np.linspace(0.1, 4.9, 8)],
            dtype=float,
        )
        remote_layer = roof + np.asarray([0, 0, 3], dtype=float)
        result = validate_facet_points(
            np.vstack([roof, remote_layer]),
            [
                {
                    "facetId": "F1",
                    "verticesMeters": [[0, 0, 3], [10, 0, 3], [10, 5, 5.5], [0, 5, 5.5]],
                    "normal": [0, -0.5, 1],
                }
            ],
            settings(),
        )
        self.assertEqual(result["facets"][0]["supportPoints"], len(roof))
        self.assertEqual(result["facets"][0]["discardedBeyondPlaneDistance"], len(remote_layer))

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
