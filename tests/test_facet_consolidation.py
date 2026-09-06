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
    _partition_roofer_facet,
    discover_consolidated_planes,
    validate_solar_dsm_support,
)

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
        self.assertEqual(expected_audit["consolidatedPlaneCount"], 2)
        self.assertEqual(actual_audit["consolidatedPlaneCount"], 2)

    def test_plane_intersection_partitions_one_roofer_face_without_gaps(self):
        first = ConsolidatedPlane(
            point_indexes=tuple(range(100)),
            normal=(-0.4472135955, 0.0, 0.894427191),
            centroid=(1.0, 2.0, 0.5),
            rmse_meters=0.0,
            support_hull=Polygon([(0, 0), (2, 0), (2, 4), (0, 4)]),
        )
        second = ConsolidatedPlane(
            point_indexes=tuple(range(100, 200)),
            normal=(0.4472135955, 0.0, 0.894427191),
            centroid=(3.0, 2.0, 0.5),
            rmse_meters=0.0,
            support_hull=Polygon([(2, 0), (4, 0), (4, 4), (2, 4)]),
        )
        roof = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
        regions = _partition_roofer_facet(roof, [first, second])
        self.assertEqual(len(regions), 2)
        self.assertAlmostEqual(sum(region.area for region, _ in regions), roof.area)


if __name__ == "__main__":
    unittest.main()
