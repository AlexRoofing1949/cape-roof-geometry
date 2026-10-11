"""Quality metadata is evidence, not a truthy/falsy default."""
import unittest
from app.cityjson_geometry import _quality_confidence
from app.errors import UnreliableGeometryError

class QualityEvidenceTests(unittest.TestCase):
    def quality(self, **updates):
        evidence={'rf_pt_density':20, 'rf_nodata_frac':0, 'rf_rmse_lod22':0}
        evidence.update(updates)
        return _quality_confidence(evidence, 8, 0.1, 0.35)

    def test_perfect_zero_error_and_missing_fraction_are_valid(self):
        confidence, components=self.quality()
        self.assertEqual(confidence, 1)
        self.assertEqual(components, {'density':1,'coverage':1,'rmse':1})

    def test_numeric_zero_strings_are_valid(self):
        self.assertEqual(self.quality(rf_nodata_frac='0',rf_rmse_lod22='0.0')[0],1)

    def test_invalid_evidence_is_rejected_without_defaulting(self):
        for field in ('rf_pt_density','rf_nodata_frac','rf_rmse_lod22'):
            for value in (None, '', ' ', True, False, -1, float('nan'), float('inf'), 'garbage', [], {}):
                with self.subTest(field=field,value=value):
                    with self.assertRaises(UnreliableGeometryError) as caught:
                        self.quality(**{field:value})
                    self.assertEqual(caught.exception.code,'ROOFER_QUALITY_METADATA_INVALID')
                    self.assertEqual(caught.exception.details, {'field':field})

    def test_missing_fields_rejected(self):
        for field in ('rf_pt_density','rf_nodata_frac','rf_rmse_lod22'):
            evidence={'rf_pt_density':20,'rf_nodata_frac':0,'rf_rmse_lod22':0}
            del evidence[field]
            with self.subTest(field=field), self.assertRaises(UnreliableGeometryError):
                _quality_confidence(evidence,8,0.1,0.35)

    def test_nodata_fraction_over_one_is_invalid(self):
        with self.assertRaises(UnreliableGeometryError) as caught:
            self.quality(rf_nodata_frac=1.01)
        self.assertEqual(caught.exception.code,'ROOFER_QUALITY_METADATA_INVALID')

    def test_existing_thresholds_still_enforced(self):
        for field,value,code in [('rf_pt_density',0,'LIDAR_DENSITY_TOO_LOW'),
            ('rf_nodata_frac',0.11,'LIDAR_COVERAGE_INCOMPLETE'),
            ('rf_rmse_lod22',0.36,'ROOFER_RMSE_TOO_HIGH')]:
            with self.subTest(field=field):
                with self.assertRaises(UnreliableGeometryError) as caught:
                    self.quality(**{field:value})
                self.assertEqual(caught.exception.code,code)

if __name__=='__main__':
    unittest.main()
