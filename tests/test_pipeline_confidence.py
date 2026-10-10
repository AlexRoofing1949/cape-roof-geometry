import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.errors import UnreliableGeometryError
from app.pipeline import (
    _combined_confidence,
    _confidence_diagnostics,
    _extract_roof_geometry_with_coverage_recovery,
    _enforce_roofprint_perimeter_consistency,
    _enforce_shared_boundary_completeness,
)


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

    def test_confidence_failure_diagnostics_are_calibration_safe(self):
        geometry = {
            "roofAreaSqFt": 6943.33,
            "averagePitchDegrees": 26.37,
            "rakesFeet": 389.13,
            "eavesFeet": 374.75,
            "facets": [{"verticesMeters": [[1, 2, 3]]}] * 16,
            "quality": {"rmseMeters": 0.2},
            "independentPlaneValidation": {
                "validation": "PASSED",
                "rawPoints": [[1, 2, 3]],
            },
        }
        result = _confidence_diagnostics(
            geometry,
            {"areaVariancePercent": 0.82},
            {
                "verificationStatus": "INSPECTION_REQUIRED",
                "pricingAllowed": False,
                "status": "LIDAR_TILE_DATE_UNAVAILABLE",
                "warnings": ["DATE_REQUIRED"],
                "currentImagery": {
                    "sourceId": "public-current-imagery",
                    "captureDate": "2026-03-22",
                    "captureDatePrecision": "PROJECT_WINDOW_END",
                    "footprintIou": 0.8132,
                    "centroidShiftMeters": 2.113,
                    "areaChangePercent": 9.412,
                    "providerFeatureId": "75249",
                    "validation": "NOT_RUN",
                    "rawImage": "must-not-be-returned",
                },
            },
            SimpleNamespace(
                source_id="usgs-public-lidar", acquired_end="2019-03-31"
            ),
            {"tileAcquisitionDate": ""},
            12.3456,
        )

        self.assertEqual(result["geometry"]["facetCount"], 16)
        self.assertEqual(result["geometry"]["rakesFeet"], 389.13)
        self.assertNotIn("facets", result["geometry"])
        self.assertEqual(
            result["geometry"]["independentPlaneValidation"], "PASSED"
        )
        self.assertNotIn("rawImage", result["currentStructure"]["currentImagery"])
        self.assertEqual(
            result["currentStructure"]["currentImagery"]["footprintIou"],
            0.8132,
        )
        self.assertEqual(
            result["currentStructure"]["currentImagery"]["providerFeatureId"],
            "75249",
        )
        self.assertEqual(
            result["pointCloud"]["acquisitionReferenceDate"], "2019-03-31"
        )
        self.assertEqual(
            result["pointCloud"]["acquisitionDatePrecision"],
            "REGISTERED_PROJECT_WINDOW_END",
        )
        self.assertEqual(result["pointCloud"]["pointDensityPpsm"], 12.346)

    def test_roofprint_perimeter_reconciliation_passes_without_inference(self):
        geometry = {"externalProjectedPerimeterFeet": 100.0}
        result = _enforce_roofprint_perimeter_consistency(
            geometry,
            SimpleNamespace(length=30.48),
            10.0,
        )

        self.assertAlmostEqual(result["variancePercent"], 0.0, delta=0.001)
        self.assertEqual(geometry["roofprintPerimeterReconciliation"], result)
        self.assertEqual(result["topology"]["nodedEdgeCount"], None)

    def test_roofprint_perimeter_mismatch_fails_closed(self):
        geometry = {"externalProjectedPerimeterFeet": 145.0}

        with self.assertRaises(UnreliableGeometryError) as context:
            _enforce_roofprint_perimeter_consistency(
                geometry,
                SimpleNamespace(length=30.48),
                10.0,
            )

        self.assertEqual(context.exception.code, "ROOF_TOPOLOGY_PERIMETER_MISMATCH")
        self.assertEqual(context.exception.details["variancePercent"], 45.0)
        self.assertNotIn("roofprintPerimeterReconciliation", geometry)

    def test_small_unmatched_boundary_is_audited_without_inventing_an_edge(self):
        geometry = {
            "topology": {
                "unmatchedInteriorBoundaryCount": 1,
                "unmatchedInteriorBoundaryFeet": 3.25,
            }
        }

        result = _enforce_shared_boundary_completeness(geometry, 5.0)

        self.assertEqual(result["unmatchedInteriorBoundaryFeet"], 3.25)
        self.assertEqual(geometry["sharedBoundaryCompleteness"], result)

    def test_material_unmatched_boundaries_fail_closed(self):
        geometry = {
            "topology": {
                "unmatchedInteriorBoundaryCount": 4,
                "unmatchedInteriorBoundaryFeet": 18.75,
                "offsetBoundaryCandidateCount": 7,
                "repairedSharedBoundaryCount": 2,
                "offsetBoundaryRejectionCounts": {
                    "BOUNDARY_DIRECTION_MISMATCH": 5
                },
            }
        }

        with self.assertRaises(UnreliableGeometryError) as context:
            _enforce_shared_boundary_completeness(geometry, 5.0)

        self.assertEqual(
            context.exception.code,
            "ROOF_TOPOLOGY_SHARED_BOUNDARY_INCOMPLETE",
        )
        self.assertEqual(
            context.exception.details["unmatchedInteriorBoundaryFeet"], 18.75
        )
        self.assertEqual(
            context.exception.details["offsetBoundaryCandidateCount"], 7
        )
        self.assertEqual(
            context.exception.details["repairedSharedBoundaryCount"], 2
        )
        self.assertEqual(
            context.exception.details["offsetBoundaryRejectionCounts"],
            {"BOUNDARY_DIRECTION_MISMATCH": 5},
        )

    def test_marginal_coverage_failure_retries_once_with_audited_limit(self):
        recovered = {"quality": {}, "confidence": 0.91}
        with patch(
            "app.pipeline.extract_roof_geometry",
            side_effect=[
                UnreliableGeometryError(
                    "LIDAR_COVERAGE_INCOMPLETE",
                    "missing coverage",
                    details={"noDataFraction": 0.1019, "maximumNoDataFraction": 0.10},
                ),
                recovered,
            ],
        ) as extract:
            result = _extract_roof_geometry_with_coverage_recovery(
                {"CityObjects": {}},
                None,
                SimpleNamespace(
                    maximum_nodata_fraction=0.10,
                    maximum_recoverable_nodata_fraction=0.15,
                ),
            )

        self.assertIs(result, recovered)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(extract.call_args_list[0].kwargs["maximum_nodata_fraction"], 0.10)
        self.assertEqual(extract.call_args_list[1].kwargs["maximum_nodata_fraction"], 0.15)
        self.assertTrue(extract.call_args_list[1].kwargs["observed_coverage_score"])
        self.assertEqual(
            result["quality"]["coverageRecovery"]["status"], "BOUNDED_RECOVERY"
        )
        self.assertEqual(
            result["quality"]["coverageRecovery"]["observedNoDataFraction"], 0.1019
        )

    def test_coverage_recovery_does_not_retry_other_or_excessive_failures(self):
        other = UnreliableGeometryError("ROOFER_RMSE_TOO_HIGH", "rmse")
        with patch("app.pipeline.extract_roof_geometry", side_effect=other) as extract:
            with self.assertRaises(UnreliableGeometryError) as context:
                _extract_roof_geometry_with_coverage_recovery(
                    {},
                    None,
                    SimpleNamespace(
                        maximum_nodata_fraction=0.10,
                        maximum_recoverable_nodata_fraction=0.15,
                    ),
                )
        self.assertIs(context.exception, other)
        extract.assert_called_once()

        excessive = UnreliableGeometryError(
            "LIDAR_COVERAGE_INCOMPLETE",
            "missing coverage",
            details={"noDataFraction": 0.2001, "maximumNoDataFraction": 0.10},
        )
        with patch("app.pipeline.extract_roof_geometry", side_effect=excessive) as extract:
            with self.assertRaises(UnreliableGeometryError) as context:
                _extract_roof_geometry_with_coverage_recovery(
                    {},
                    None,
                    SimpleNamespace(
                        maximum_nodata_fraction=0.10,
                        maximum_recoverable_nodata_fraction=0.15,
                    ),
                )
        self.assertIs(context.exception, excessive)
        extract.assert_called_once()


if __name__ == "__main__":
    unittest.main()
