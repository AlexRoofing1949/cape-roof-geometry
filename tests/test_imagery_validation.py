import io
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from shapely.geometry import Polygon, mapping

from app.imagery_validation import (
    SQUARE_METERS_TO_SQUARE_FEET,
    _arcgis_building_validation,
    _imagery_evidence_calibration_failures,
    validate_current_structure,
)
from app.providers import transform_geometry, utm_epsg


class SolarBuildingModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.geometry_wgs84 = Polygon(
            [
                (-81.95860, 26.63255),
                (-81.95845, 26.63255),
                (-81.95845, 26.63267),
                (-81.95860, 26.63267),
            ]
        )
        self.footprint = SimpleNamespace(
            geometry_wgs84=self.geometry_wgs84,
            overture_id="building-test",
        )
        epsg = utm_epsg(float(self.geometry_wgs84.centroid.x), float(self.geometry_wgs84.centroid.y))
        footprint_m = transform_geometry(self.geometry_wgs84, "EPSG:4326", f"EPSG:{epsg}")
        self.ground_area_sqft = footprint_m.area * SQUARE_METERS_TO_SQUARE_FEET
        today = datetime.now(timezone.utc).date()
        self.imagery_date = today.replace(year=today.year - 1)
        self.lidar_date = today.replace(year=today.year - 2)
        self.lidar = SimpleNamespace(
            tile_acquisition_date=self.lidar_date.isoformat(),
            acquired_end=self.lidar_date.isoformat(),
            age_years=3,
        )
        self.solar = SimpleNamespace(
            imageryDate=self.imagery_date.isoformat(),
            imageryQuality="HIGH",
            roofAreaSqFt=2000.0,
            averagePitchDegrees=20.0,
            facets=[
                SimpleNamespace(groundAreaSqFt=self.ground_area_sqft / 2),
                SimpleNamespace(groundAreaSqFt=self.ground_area_sqft / 2),
            ],
        )
        self.reconstruction = {
            "roofAreaSqFt": 2000.0,
            "averagePitchDegrees": 20.0,
            "facets": [{}, {}],
        }
        self.registries = SimpleNamespace(imagery_sources=())

    def validate(self, solar=None):
        return validate_current_structure(
            self.footprint,
            self.lidar,
            "Lee",
            self.registries,
            current_lidar_max_age_years=2,
            maximum_current_imagery_age_years=2,
            allow_historical_verified_pricing=True,
            solar_reference=solar or self.solar,
            reconstructed_geometry=self.reconstruction,
            maximum_area_variance_percent=15,
            maximum_pitch_variance_degrees=10,
        )

    def test_high_quality_newer_solar_model_can_verify_historical_geometry(self):
        result = self.validate()
        self.assertEqual(result["verificationStatus"], "VERIFIED_HISTORICAL_UNCHANGED")
        self.assertTrue(result["pricingAllowed"])
        self.assertEqual(result["currentImagery"]["validation"], "PASSED")
        self.assertEqual(result["currentImagery"]["captureDatePrecision"], "PROVIDER_APPROXIMATE_DAY")
        self.assertEqual(result["currentImagery"]["attribution"], "Includes data from Google Maps")
        self.assertEqual(
            result["currentImagery"]["lidarReferenceDatePrecision"],
            "EXACT_GPS_DATE",
        )

    def test_registered_project_window_end_can_be_used_conservatively(self):
        self.lidar.tile_acquisition_date = ""

        result = self.validate()

        self.assertEqual(result["verificationStatus"], "VERIFIED_HISTORICAL_UNCHANGED")
        self.assertTrue(result["pricingAllowed"])
        self.assertEqual(
            result["currentImagery"]["lidarReferenceDate"],
            self.lidar.acquired_end,
        )
        self.assertEqual(
            result["currentImagery"]["lidarReferenceDatePrecision"],
            "REGISTERED_PROJECT_WINDOW_END",
        )
        self.assertIn(
            "LIDAR_REFERENCE_USES_REGISTERED_PROJECT_WINDOW_END",
            result["warnings"],
        )

    def test_area_change_fails_closed(self):
        changed = SimpleNamespace(**vars(self.solar))
        changed.facets = [SimpleNamespace(groundAreaSqFt=self.ground_area_sqft * 1.5)]
        result = self.validate(changed)
        self.assertEqual(result["verificationStatus"], "INSPECTION_REQUIRED")
        self.assertFalse(result["pricingAllowed"])
        self.assertEqual(result["status"], "STRUCTURE_CHANGED_AFTER_LIDAR")
        self.assertIn("FOOTPRINT_AREA_CHANGED", result["warnings"])

    def test_stale_solar_imagery_fails_closed(self):
        stale = SimpleNamespace(**vars(self.solar))
        stale.imageryDate = self.imagery_date.replace(year=self.imagery_date.year - 3).isoformat()
        result = self.validate(stale)
        self.assertEqual(result["verificationStatus"], "INSPECTION_REQUIRED")
        self.assertFalse(result["pricingAllowed"])
        self.assertEqual(result["status"], "CURRENT_IMAGERY_INSUFFICIENT")
        self.assertIn("IMAGERY_TOO_OLD_FOR_CURRENT_VALIDATION", result["warnings"])

    @patch("app.imagery_validation.urllib.request.urlopen")
    def test_official_current_building_footprint_can_verify_unchanged_structure(self, urlopen):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "OBJECTID": 75249,
                        "BldgDataSource": "LeePA Building Footprints",
                        "last_edited_date": int(datetime.now(timezone.utc).timestamp() * 1000),
                    },
                    "geometry": mapping(self.geometry_wgs84),
                }
            ],
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        source = SimpleNamespace(
            id="lee_county_2026_building_evidence",
            evidence_endpoint="https://example.invalid/query",
            imagery_endpoint="https://example.invalid/imagery",
            capture_start=self.lidar_date,
            capture_end=self.imagery_date,
            gsd_meters=0.0762,
            license="LEE_COUNTY_PUBLIC_INFORMATION_RESOURCE",
            attribution="Eagle View, Lee County Property Appraiser, Lee County GIS",
        )
        result = _arcgis_building_validation(
            self.footprint,
            self.lidar,
            source,
            provider_timeout_seconds=5,
            maximum_current_imagery_age_years=2,
            current_lidar_max_age_years=2,
            allow_historical_verified_pricing=True,
        )
        self.assertEqual(result["verificationStatus"], "VERIFIED_HISTORICAL_UNCHANGED")
        self.assertTrue(result["pricingAllowed"])
        self.assertEqual(result["currentImagery"]["validation"], "PASSED")
        self.assertEqual(result["currentImagery"]["providerFeatureId"], "75249")

    def test_solar_model_is_used_when_county_geometry_does_not_match(self):
        source = SimpleNamespace(
            id="lee_county_2026_building_evidence",
            enabled=True,
            counties=("Lee",),
            commercial_estimate_use_allowed=True,
            license="LEE_COUNTY_PUBLIC_INFORMATION_RESOURCE",
            capture_end=self.imagery_date,
            evidence_file=None,
            evidence_kind="arcgis_building_footprints",
            evidence_endpoint="https://example.invalid/query",
        )
        self.registries = SimpleNamespace(imagery_sources=(source,))
        county_failure = {
            "verificationStatus": "INSPECTION_REQUIRED",
            "pricingAllowed": False,
            "status": "STRUCTURE_CHANGED_AFTER_LIDAR",
            "holdReason": "STRUCTURE_CHANGED_AFTER_LIDAR",
            "currentImagery": {
                "sourceId": source.id,
                "validation": "FAILED",
            },
            "warnings": ["FOOTPRINT_IOU_FAILED"],
        }
        solar_success = {
            "verificationStatus": "VERIFIED_HISTORICAL_UNCHANGED",
            "pricingAllowed": True,
            "status": "GEOMETRY_VERIFIED",
            "holdReason": "",
            "currentImagery": {
                "sourceId": "google_solar_building_insights",
                "validation": "PASSED",
            },
            "warnings": [],
        }

        with (
            patch(
                "app.imagery_validation._arcgis_building_validation",
                return_value=county_failure,
            ),
            patch(
                "app.imagery_validation._solar_model_validation",
                return_value=solar_success,
            ),
        ):
            result = self.validate()

        self.assertTrue(result["pricingAllowed"])
        self.assertEqual(
            result["currentImagery"]["sourceId"],
            "google_solar_building_insights",
        )
        self.assertEqual(
            result["alternateEvidence"]["sourceId"], source.id
        )
        self.assertIn(
            "COUNTY_BUILDING_EVIDENCE_REJECTED_SOLAR_MODEL_USED",
            result["warnings"],
        )

    def test_uncalibrated_segmentation_evidence_is_rejected(self):
        failures = _imagery_evidence_calibration_failures(
            {
                "modelName": "MobileSAM",
                "modelVersion": "checkpoint-1",
                "qualityPassed": True,
            }
        )
        self.assertIn("CALIBRATION_DATASET_VERSION_MISSING", failures)
        self.assertIn("CALIBRATION_METRICS_MISSING", failures)

    def test_calibrated_segmentation_evidence_meets_contract(self):
        failures = _imagery_evidence_calibration_failures(
            {
                "modelName": "MobileSAM",
                "modelVersion": "checkpoint-1",
                "calibrationDatasetVersion": "swfl-roofs-v1",
                "orthorectified": True,
                "coregistered": True,
                "shadowVegetationMasked": True,
                "calibrationMetrics": {
                    "polygonIou": 0.94,
                    "boundaryF1": 0.92,
                    "medianAreaErrorPercent": 3.1,
                    "additionDeletionPrecision": 0.93,
                    "additionDeletionRecall": 0.91,
                    "falseChangeRatePercent": 2.0,
                    "failureRatePercent": 3.0,
                },
            }
        )
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
