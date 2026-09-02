import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from shapely.geometry import Polygon

from app.imagery_validation import SQUARE_METERS_TO_SQUARE_FEET, validate_current_structure
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


if __name__ == "__main__":
    unittest.main()
