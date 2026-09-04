import math
import unittest
from copy import deepcopy
from pathlib import Path

from app.cityjson_geometry import (
    EdgeUse,
    Facet,
    _facet_side_height_delta,
    _noded_edge_uses,
    _validated_plane_intersection_edge,
    extract_roof_geometry,
    load_cityjson_feature,
)


FIXTURES = Path(__file__).parent / "fixtures"


class CityJsonGeometryTests(unittest.TestCase):
    def test_sub_decimetre_shared_edge_offsets_are_noded(self):
        first_vertices = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 5.0, 5.0))
        second_vertices = ((10.0, 0.04, 0.03), (0.0, 0.04, 0.03), (0.0, -5.0, 5.0))

        def facet(facet_id, vertices):
            return Facet(
                facet_id=facet_id,
                vertex_ids=tuple(range(len(vertices))),
                vertices=vertices,
                area_square_meters=1.0,
                horizontal_area_square_meters=1.0,
                pitch_degrees=30.0,
                azimuth_degrees=180.0,
                centroid=tuple(sum(point[i] for point in vertices) / 3 for i in range(3)),
                normal=(0.0, 0.5, math.sqrt(0.75)),
                opening_count=0,
                opening_perimeter_meters=0.0,
                semantic_attributes={},
            )

        edge_uses = _noded_edge_uses(
            [facet("F1", first_vertices), facet("F2", second_vertices)],
            tolerance_meters=0.10,
        )

        self.assertTrue(any(len(uses) == 2 for uses in edge_uses.values()))

    def test_plane_fit_height_residuals_do_not_duplicate_shared_edges(self):
        first_vertices = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 5.0, 5.0))
        second_vertices = ((10.0, 0.04, 0.24), (0.0, 0.04, 0.24), (0.0, -5.0, 5.0))

        def facet(facet_id, vertices):
            return Facet(
                facet_id=facet_id,
                vertex_ids=tuple(range(len(vertices))),
                vertices=vertices,
                area_square_meters=1.0,
                horizontal_area_square_meters=1.0,
                pitch_degrees=30.0,
                azimuth_degrees=180.0,
                centroid=tuple(sum(point[i] for point in vertices) / 3 for i in range(3)),
                normal=(0.0, 0.5, math.sqrt(0.75)),
                opening_count=0,
                opening_perimeter_meters=0.0,
                semantic_attributes={},
            )

        edge_uses = _noded_edge_uses(
            [facet("F1", first_vertices), facet("F2", second_vertices)],
            tolerance_meters=0.10,
            vertical_tolerance_meters=0.30,
        )

        self.assertTrue(any(len(uses) == 2 for uses in edge_uses.values()))

    def test_shared_edge_is_corrected_to_facet_plane_intersection(self):
        root_half = math.sqrt(0.5)
        first_vertices = (
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 5.0, 5.0),
        )
        second_vertices = (
            (10.0, 0.04, 0.24),
            (0.0, 0.04, 0.24),
            (0.0, -5.0, 5.28),
        )

        def facet(facet_id, vertices, normal):
            return Facet(
                facet_id=facet_id,
                vertex_ids=tuple(range(len(vertices))),
                vertices=vertices,
                area_square_meters=1.0,
                horizontal_area_square_meters=1.0,
                pitch_degrees=45.0,
                azimuth_degrees=180.0,
                centroid=tuple(
                    sum(point[i] for point in vertices) / 3 for i in range(3)
                ),
                normal=normal,
                opening_count=0,
                opening_perimeter_meters=0.0,
                semantic_attributes={},
            )

        first = facet("F1", first_vertices, (0.0, -root_half, root_half))
        second = facet("F2", second_vertices, (0.0, root_half, root_half))
        uses = [
            EdgeUse(first, 0, 1, first_vertices[0], first_vertices[1]),
            EdgeUse(second, 0, 1, second_vertices[0], second_vertices[1]),
        ]

        corrected, evidence = _validated_plane_intersection_edge(uses, 0.35)

        self.assertEqual(evidence["derivation"], "PLANE_PLANE_BOUNDARY_INTERSECTION")
        self.assertLessEqual(evidence["maximumCorrectionMeters"], 0.35)
        self.assertEqual(evidence["originalBoundaryAlignmentDegrees"], [0.0, 0.0])
        self.assertAlmostEqual(evidence["incidentPlaneAngleDegrees"], 90.0)
        self.assertAlmostEqual(corrected[0].start[1], 0.14, delta=0.001)
        self.assertAlmostEqual(corrected[0].start[2], 0.14, delta=0.001)
        self.assertEqual(corrected[0].start, corrected[1].end)
        self.assertEqual(corrected[0].end, corrected[1].start)
        for use in corrected:
            plane_origin = use.facet.vertices[0]
            for point in (use.start, use.end):
                self.assertAlmostEqual(
                    sum(
                        use.facet.normal[index]
                        * (point[index] - plane_origin[index])
                        for index in range(3)
                    ),
                    0.0,
                    delta=1e-9,
                )
        self.assertGreater(_facet_side_height_delta(corrected[0]), 0)
        self.assertGreater(_facet_side_height_delta(corrected[1]), 0)

    def test_vertically_separated_edges_remain_distinct(self):
        first_vertices = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 5.0, 5.0))
        second_vertices = ((10.0, 0.04, 0.80), (0.0, 0.04, 0.80), (0.0, -5.0, 5.0))

        def facet(facet_id, vertices):
            return Facet(
                facet_id=facet_id,
                vertex_ids=tuple(range(len(vertices))),
                vertices=vertices,
                area_square_meters=1.0,
                horizontal_area_square_meters=1.0,
                pitch_degrees=30.0,
                azimuth_degrees=180.0,
                centroid=tuple(sum(point[i] for point in vertices) / 3 for i in range(3)),
                normal=(0.0, 0.5, math.sqrt(0.75)),
                opening_count=0,
                opening_perimeter_meters=0.0,
                semantic_attributes={},
            )

        edge_uses = _noded_edge_uses(
            [facet("F1", first_vertices), facet("F2", second_vertices)],
            tolerance_meters=0.10,
            vertical_tolerance_meters=0.30,
        )

        self.assertFalse(any(len(uses) == 2 for uses in edge_uses.values()))

    def test_shared_edge_uses_local_interior_side_for_concave_facet(self):
        vertices = (
            (0.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (4.0, 4.0, 4.0),
            (3.0, 4.0, 4.0),
            (3.0, 1.0, 1.0),
            (1.0, 1.0, 1.0),
            (1.0, 4.0, 4.0),
            (0.0, 4.0, 4.0),
        )
        facet = Facet(
            facet_id="F1",
            vertex_ids=tuple(range(len(vertices))),
            vertices=vertices,
            area_square_meters=1.0,
            horizontal_area_square_meters=1.0,
            pitch_degrees=45.0,
            azimuth_degrees=180.0,
            centroid=(2.0, 2.25, 2.25),
            normal=(0.0, -math.sqrt(0.5), math.sqrt(0.5)),
            opening_count=0,
            opening_perimeter_meters=0.0,
            semantic_attributes={},
        )
        use = EdgeUse(facet, 4, 5, vertices[4], vertices[5])

        self.assertLess(_facet_side_height_delta(use), -0.9)

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
        self.assertAlmostEqual(result["totalHorizontalRoofAreaSqFt"], horizontal_area_square_meters * 10.763910416709722, delta=0.03)
        self.assertAlmostEqual(result["externalPerimeterFeet"], result["eavesFeet"] + result["rakesFeet"], delta=0.01)
        self.assertAlmostEqual(
            result["externalProjectedPerimeterFeet"],
            32 * 3.280839895013123,
            delta=0.03,
        )
        self.assertAlmostEqual(result["internalRoofEdgeFeet"], result["ridgesFeet"], delta=0.01)
        self.assertAlmostEqual(
            sum(facet["horizontalAreaSqFt"] for facet in result["facets"]),
            horizontal_area_square_meters * 10.763910416709722,
            delta=0.03,
        )
        for facet in result["facets"]:
            self.assertAlmostEqual(facet["areaSqFt"], facet["slopeAreaFormulaSqFt"], delta=0.02)
            self.assertAlmostEqual(facet["pitchRisePer12"], 12, delta=0.01)

    def test_facet_ids_and_edges_are_independent_of_roofer_surface_order(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        expected = extract_roof_geometry(feature, transform)
        reordered = deepcopy(feature)
        geometry = reordered["CityObjects"]["TEST-GABLE-0"]["geometry"][0]
        geometry["boundaries"][0].reverse()
        geometry["semantics"]["values"][0].reverse()
        for surface in geometry["boundaries"][0]:
            ring = surface[0]
            surface[0] = list(reversed(ring[1:] + ring[:1]))

        actual = extract_roof_geometry(reordered, transform)

        self.assertEqual(actual, expected)

    def test_coincident_ids_and_t_junction_are_noded_before_edge_classification(self):
        feature = {
            "type": "CityJSONFeature",
            "id": "NODED-GABLE",
            "vertices": [
                [0, 0, 0], [10, 0, 0], [10, 5, 5], [0, 5, 5],
                [0, 5, 5], [5, 5, 5], [5, 10, 0], [0, 10, 0],
                [5, 5, 5], [10, 5, 5], [10, 10, 0], [5, 10, 0],
            ],
            "CityObjects": {
                "NODED-GABLE": {
                    "type": "Building",
                    "attributes": {
                        "rf_success": True,
                        "rf_pointcloud_unusable": False,
                        "rf_extrusion_mode": "standard",
                        "rf_pt_density": 15,
                        "rf_nodata_frac": 0.01,
                        "rf_rmse_lod22": 0.1,
                    },
                    "geometry": [
                        {
                            "type": "MultiSurface",
                            "lod": "2.2",
                            "boundaries": [
                                [[0, 1, 2, 3]],
                                [[4, 5, 6, 7]],
                                [[8, 9, 10, 11]],
                            ],
                            "semantics": {
                                "surfaces": [{"type": "RoofSurface"}] * 3,
                                "values": [0, 1, 2],
                            },
                        }
                    ],
                }
            },
        }

        result = extract_roof_geometry(feature, None)

        self.assertAlmostEqual(result["ridgesFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertAlmostEqual(result["eavesFeet"], 20 * 3.280839895013123, delta=0.02)
        self.assertEqual(result["highPerimeterFeet"], 0)
        self.assertEqual(len(result["ridges"]), 2)

    def test_one_decisive_shared_edge_side_classifies_tangent_valley(self):
        feature = {
            "type": "CityJSONFeature",
            "id": "TANGENT-VALLEY",
            "vertices": [
                [0, 0, 1], [10, 0, 1], [10, 5, 1], [0, 5, 1],
                [0, -5, 3], [10, -5, 3],
            ],
            "CityObjects": {
                "TANGENT-VALLEY": {
                    "type": "Building",
                    "attributes": {
                        "rf_success": True,
                        "rf_pointcloud_unusable": False,
                        "rf_extrusion_mode": "standard",
                        "rf_pt_density": 15,
                        "rf_nodata_frac": 0.01,
                        "rf_rmse_lod22": 0.1,
                    },
                    "geometry": [{
                        "type": "MultiSurface",
                        "lod": "2.2",
                        "boundaries": [[[0, 1, 2, 3]], [[1, 0, 4, 5]]],
                        "semantics": {
                            "surfaces": [
                                {"type": "RoofSurface", "rf_slope": 0},
                                {"type": "RoofSurface", "rf_slope": 21.801409},
                            ],
                            "values": [0, 1],
                        },
                    }],
                }
            },
        }

        result = extract_roof_geometry(feature, None)

        self.assertAlmostEqual(result["valleysFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertEqual(result["ridgesFeet"], 0)

    def test_one_decisive_shared_edge_side_classifies_tangent_ridge(self):
        feature = {
            "type": "CityJSONFeature",
            "id": "TANGENT-RIDGE",
            "vertices": [
                [0, 0, 1], [10, 0, 1], [10, 5, 1], [0, 5, 1],
                [0, -5, -1], [10, -5, -1],
            ],
            "CityObjects": {
                "TANGENT-RIDGE": {
                    "type": "Building",
                    "attributes": {
                        "rf_success": True,
                        "rf_pointcloud_unusable": False,
                        "rf_extrusion_mode": "standard",
                        "rf_pt_density": 15,
                        "rf_nodata_frac": 0.01,
                        "rf_rmse_lod22": 0.1,
                    },
                    "geometry": [{
                        "type": "MultiSurface",
                        "lod": "2.2",
                        "boundaries": [[[0, 1, 2, 3]], [[1, 0, 4, 5]]],
                        "semantics": {
                            "surfaces": [
                                {"type": "RoofSurface", "rf_slope": 0},
                                {"type": "RoofSurface", "rf_slope": 21.801409},
                            ],
                            "values": [0, 1],
                        },
                    }],
                }
            },
        }

        result = extract_roof_geometry(feature, None)

        self.assertAlmostEqual(result["ridgesFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertEqual(result["valleysFeet"], 0)

    def test_low_density_fails_closed(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        feature["CityObjects"]["TEST-GABLE"]["attributes"]["rf_pt_density"] = 2
        with self.assertRaisesRegex(Exception, "point density") as context:
            extract_roof_geometry(feature, transform)
        self.assertEqual(context.exception.code, "LIDAR_DENSITY_TOO_LOW")
        self.assertEqual(
            context.exception.details,
            {"pointDensityPpsm": 2.0, "minimumPointDensityPpsm": 8.0},
        )

    def test_excessive_nodata_fails_closed_with_sanitized_metrics(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        feature["CityObjects"]["TEST-GABLE"]["attributes"]["rf_nodata_frac"] = 0.25
        with self.assertRaisesRegex(Exception, "missing coverage") as context:
            extract_roof_geometry(feature, transform)
        self.assertEqual(context.exception.code, "LIDAR_COVERAGE_INCOMPLETE")
        self.assertEqual(
            context.exception.details,
            {"noDataFraction": 0.25, "maximumNoDataFraction": 0.10},
        )

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

    def test_roof_opening_area_is_subtracted_without_misclassifying_its_edges(self):
        feature = {
            "type": "CityJSONFeature",
            "id": "OPENING",
            "vertices": [
                [0, 0, 3],
                [10, 0, 3],
                [10, 10, 3],
                [0, 10, 3],
                [4, 4, 3],
                [6, 4, 3],
                [6, 6, 3],
                [4, 6, 3],
            ],
            "CityObjects": {
                "OPENING": {
                    "type": "Building",
                    "attributes": {
                        "rf_success": True,
                        "rf_pointcloud_unusable": False,
                        "rf_extrusion_mode": "standard",
                        "rf_pt_density": 15,
                        "rf_nodata_frac": 0.01,
                        "rf_rmse_lod22": 0.1,
                    },
                    "geometry": [
                        {
                            "type": "MultiSurface",
                            "lod": "2.2",
                            "boundaries": [[[0, 1, 2, 3], [4, 7, 6, 5]]],
                            "semantics": {
                                "surfaces": [{"type": "RoofSurface", "rf_slope": 0}],
                                "values": [0],
                            },
                        }
                    ],
                }
            },
        }

        result = extract_roof_geometry(feature, None)

        self.assertAlmostEqual(result["roofAreaSqFt"], 96 * 10.763910416709722, delta=0.02)
        self.assertEqual(result["roofOpeningCount"], 1)
        self.assertAlmostEqual(result["roofOpeningPerimeterFeet"], 8 * 3.280839895013123, delta=0.02)
        self.assertAlmostEqual(result["eavesFeet"], 40 * 3.280839895013123, delta=0.02)

    def test_high_side_is_measured_as_perimeter_flashing_at_any_pitch(self):
        feature = {
            "type": "CityJSONFeature",
            "id": "LOW-SLOPE",
            "vertices": [[0, 0, 3], [10, 0, 3], [10, 10, 5], [0, 10, 5]],
            "CityObjects": {
                "LOW-SLOPE": {
                    "type": "Building",
                    "attributes": {
                        "rf_success": True,
                        "rf_pointcloud_unusable": False,
                        "rf_extrusion_mode": "standard",
                        "rf_pt_density": 15,
                        "rf_nodata_frac": 0.01,
                        "rf_rmse_lod22": 0.1,
                    },
                    "geometry": [
                        {
                            "type": "MultiSurface",
                            "lod": "2.2",
                            "boundaries": [[[0, 1, 2, 3]]],
                            "semantics": {"surfaces": [{"type": "RoofSurface"}], "values": [0]},
                        }
                    ],
                }
            },
        }

        result = extract_roof_geometry(feature, None)

        self.assertGreater(result["averagePitchDegrees"], 5)
        self.assertAlmostEqual(result["eavesFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertAlmostEqual(result["highPerimeterFeet"], 10 * 3.280839895013123, delta=0.02)
        self.assertAlmostEqual(
            result["rakesFeet"], 2 * math.sqrt(10**2 + 2**2) * 3.280839895013123, delta=0.05
        )

    def test_missing_transform_is_allowed_for_unquantized_fixture(self):
        feature, transform = load_cityjson_feature(FIXTURES / "simple_gable.city.jsonl")
        self.assertIsNone(transform)
        result = extract_roof_geometry(feature, transform)
        self.assertGreater(result["roofAreaSqFt"], 0)


if __name__ == "__main__":
    unittest.main()
