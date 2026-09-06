from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from shapely.geometry import Polygon

from app.facet_consolidation import (
    ConsolidatedPlane,
    DsmResidualSample,
    _partition_roofer_facet,
    _spatial_dsm_residual_components,
    _temporal_evidence_role,
    _validate_watertight_partition,
    discover_consolidated_planes,
    validate_solar_dsm_support,
)
from app.errors import UnreliableGeometryError

try:
    from osgeo import gdal, osr

    SPATIAL_RUNTIME_AVAILABLE = True
except ImportError:
    SPATIAL_RUNTIME_AVAILABLE = False


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        facet_consolidation_crop_buffer_meters=0.05,
        facet_consolidation_normal_radius_meters=0.65,
        facet_consolidation_neighbor_radius_meters=0.35,
        facet_consolidation_maximum_normal_angle_degrees=4.0,
        facet_consolidation_maximum_local_residual_meters=0.03,
        facet_consolidation_minimum_points=20,
        facet_consolidation_maximum_plane_rmse_meters=0.03,
        facet_consolidation_merge_angle_degrees=3.0,
        facet_consolidation_merge_plane_distance_meters=0.05,
        facet_consolidation_merge_gap_meters=0.50,
        facet_consolidation_minimum_support_fraction=0.75,
    )


def _gable_points() -> np.ndarray:
    values = []
    for x in np.arange(0.1, 5.0, 0.2):
        for y in np.arange(0.1, 4.0, 0.2):
            z = y * 0.5 if y <= 2.0 else (4.0 - y) * 0.5
            values.append((x, y, z))
    return np.asarray(values, dtype=float)


class FacetConsolidationTests(unittest.TestCase):
    def test_dsm_residuals_cluster_only_spatially_coherent_same_sign_samples(self):
        samples = [
            DsmResidualSample(1, index, x, y, 3.0, residual)
            for index, (x, y, residual) in enumerate(
                [
                    (0.0, 0.0, 0.8),
                    (0.3, 0.0, 0.9),
                    (0.3, 0.3, 0.85),
                    (4.0, 4.0, 0.9),
                    (0.1, 0.1, -0.8),
                    (2.0, 2.0, 0.1),
                ]
            )
        ]
        components = _spatial_dsm_residual_components(
            samples,
            np.asarray([sample.dsm_minus_lidar for sample in samples]),
            residual_threshold_meters=0.35,
            maximum_gap_meters=0.50,
        )
        self.assertEqual(components, [[0, 1, 2], [4], [3]])

    def test_older_solar_evidence_cannot_veto_newer_lidar(self):
        result = _temporal_evidence_role("2018-01-15", "2019-03-31")
        self.assertEqual(result["role"], "HISTORICAL_CORROBORATION_ONLY")
        self.assertFalse(result["mayVetoNewerLidar"])

    def test_same_day_solar_evidence_may_validate_lidar(self):
        result = _temporal_evidence_role("2019-03-31", "2019-03-31")
        self.assertEqual(result["role"], "VALIDATION")
        self.assertTrue(result["mayVetoNewerLidar"])

    def test_undated_facet_evidence_fails_closed(self):
        with self.assertRaises(UnreliableGeometryError) as context:
            _temporal_evidence_role(None, "2019-03-31")
        self.assertEqual(context.exception.code, "FACET_EVIDENCE_DATE_INVALID")

    @unittest.skipUnless(SPATIAL_RUNTIME_AVAILABLE, "GDAL is not installed")
    def test_solar_dsm_shape_is_reconciled_after_vertical_offset(self):
        normal = np.asarray((-0.2, 0.0, 1.0), dtype=float)
        normal /= np.linalg.norm(normal)
        plane = ConsolidatedPlane(
            point_indexes=tuple(range(100)),
            normal=tuple(float(value) for value in normal),
            centroid=(2.5, 2.5, 0.5),
            rmse_meters=0.0,
            support_hull=Polygon([(1, 1), (4, 1), (4, 4), (1, 4)]),
            support_coordinates=tuple(
                (x, y, x * 0.2)
                for x in (1.5, 2.0, 2.5, 3.0, 3.5)
                for y in (1.5, 2.5, 3.5)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dsm.tif"
            dataset = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Float32)
            dataset.SetGeoTransform((0, 0.5, 0, 10, 0, -0.5))
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(32617)
            dataset.SetProjection(spatial_reference.ExportToWkt())
            values = np.zeros((20, 20), dtype=np.float32)
            for row in range(20):
                for column in range(20):
                    x = (column + 0.5) * 0.5
                    values[row, column] = 10.0 + x * 0.2
            dataset.GetRasterBand(1).WriteArray(values)
            dataset = None
            configured = SimpleNamespace(
                solar_dsm_minimum_sample_coverage=0.80,
                solar_dsm_maximum_centered_rmse_meters=0.10,
            )
            result = validate_solar_dsm_support(
                [plane], path, "EPSG:32617", configured
            )
        self.assertEqual(result["validation"], "PASSED")
        self.assertLess(result["centeredRmseMeters"], 0.10)

    def test_connected_plane_discovery_is_order_independent(self):
        points = _gable_points()
        footprint = Polygon([(0, 0), (5, 0), (5, 4), (0, 4)])
        expected, expected_audit = discover_consolidated_planes(
            points, footprint, _settings()
        )
        indexes = list(range(len(points)))
        random.Random(1949).shuffle(indexes)
        actual, actual_audit = discover_consolidated_planes(
            points[indexes], footprint, _settings()
        )
        self.assertEqual(len(expected), 2)
        self.assertEqual(len(actual), 2)
        self.assertEqual(
            [tuple(round(value, 6) for value in plane.normal) for plane in expected],
            [tuple(round(value, 6) for value in plane.normal) for plane in actual],
        )
        self.assertEqual(
            [plane.support_coordinates for plane in expected],
            [plane.support_coordinates for plane in actual],
        )
        self.assertEqual(expected_audit["consolidatedPlaneCount"], 2)
        self.assertEqual(actual_audit["consolidatedPlaneCount"], 2)

    def test_small_coplanar_fragment_strengthens_existing_facet_support(self):
        points = _gable_points()
        fragment = np.asarray(
            [
                (5.4 + column * 0.2, 0.2 + row * 0.2, (0.2 + row * 0.2) * 0.5)
                for column in range(2)
                for row in range(5)
            ],
            dtype=float,
        )
        planes, audit = discover_consolidated_planes(
            np.vstack((points, fragment)),
            Polygon([(0, 0), (6, 0), (6, 4), (0, 4)]),
            _settings(),
        )
        self.assertEqual(len(planes), 2)
        self.assertGreater(audit["absorbedSmallFragmentPointCount"], 0)
        self.assertGreater(
            sum(
                point_index >= len(points)
                for plane in planes
                for point_index in plane.point_indexes
            ),
            0,
        )

    def test_plane_intersection_partitions_one_roofer_face_without_gaps(self):
        first = ConsolidatedPlane(
            point_indexes=tuple(range(100)),
            normal=(-0.4472135955, 0.0, 0.894427191),
            centroid=(500001.0, 2900002.0, 0.5),
            rmse_meters=0.0,
            support_hull=Polygon(
                [
                    (500000, 2900000),
                    (500002, 2900000),
                    (500002, 2900004),
                    (500000, 2900004),
                ]
            ),
        )
        second = ConsolidatedPlane(
            point_indexes=tuple(range(100, 200)),
            normal=(0.4472135955, 0.0, 0.894427191),
            centroid=(500003.0, 2900002.0, 0.5),
            rmse_meters=0.0,
            support_hull=Polygon(
                [
                    (500002, 2900000),
                    (500004, 2900000),
                    (500004, 2900004),
                    (500002, 2900004),
                ]
            ),
        )
        roof = Polygon(
            [
                (500000, 2900000),
                (500004, 2900000),
                (500004, 2900004),
                (500000, 2900004),
            ]
        )
        lidar_points = np.asarray(
            [
                (500000.5, 2900001.0, 0.25),
                (500001.5, 2900003.0, 0.75),
                (500002.5, 2900001.0, 0.75),
                (500003.5, 2900003.0, 0.25),
            ],
            dtype=float,
        )
        assignment_audit = []
        regions = _partition_roofer_facet(
            roof,
            [first, second],
            lidar_points=lidar_points,
            assignment_audit=assignment_audit,
        )
        self.assertEqual(len(regions), 2)
        self.assertAlmostEqual(sum(region.area for region, _ in regions), roof.area)
        self.assertTrue(assignment_audit)
        self.assertTrue(
            all("cellLidarResidualRmseMeters" in item for item in assignment_audit)
        )
        manifold = _validate_watertight_partition(regions, roof)
        self.assertEqual(manifold["validation"], "PASSED")
        self.assertEqual(manifold["interiorOwnership"], 2)

    def test_watertight_gate_rejects_overlapping_facets(self):
        plane = ConsolidatedPlane(
            point_indexes=tuple(range(20)),
            normal=(0.0, 0.0, 1.0),
            centroid=(1.0, 1.0, 0.0),
            rmse_meters=0.0,
            support_hull=Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
        )
        roof = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
        with self.assertRaises(UnreliableGeometryError) as context:
            _validate_watertight_partition(
                [(roof, plane), (Polygon([(1, 0), (2, 0), (2, 2), (1, 2)]), plane)],
                roof,
            )
        self.assertEqual(
            context.exception.code, "FACET_GLOBAL_ARRANGEMENT_INCOMPLETE"
        )

    def test_watertight_gate_uses_boundary_ownership_at_large_coordinates(self):
        plane = ConsolidatedPlane(
            point_indexes=tuple(range(20)),
            normal=(0.0, 0.0, 1.0),
            centroid=(500002.0, 2900002.0, 0.0),
            rmse_meters=0.0,
            support_hull=Polygon(
                [
                    (500000, 2900000),
                    (500004, 2900000),
                    (500004, 2900004),
                    (500000, 2900004),
                ]
            ),
        )
        roof = plane.support_hull
        # Deliberately node the common boundary into sub-millimetre pieces.
        split_y = 2900002.00015
        regions = [
            (
                Polygon(
                    [
                        (500000, 2900000),
                        (500002, 2900000),
                        (500002, split_y),
                        (500002, 2900004),
                        (500000, 2900004),
                    ]
                ),
                plane,
            ),
            (
                Polygon(
                    [
                        (500002, 2900000),
                        (500004, 2900000),
                        (500004, 2900004),
                        (500002, 2900004),
                        (500002, split_y),
                    ]
                ),
                plane,
            ),
        ]
        manifold = _validate_watertight_partition(regions, roof)
        self.assertEqual(manifold["validation"], "PASSED")
        self.assertEqual(manifold["interiorOwnership"], 2)


if __name__ == "__main__":
    unittest.main()
