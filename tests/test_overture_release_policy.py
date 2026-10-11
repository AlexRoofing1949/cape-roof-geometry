"""Release policy tests use synthetic data and never contact providers."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from shapely.geometry import box, mapping
from app.errors import UnreliableGeometryError
from app.providers import _download_overture_release, fetch_overture_footprint

class OvertureReleasePolicyTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(overture_release='2026-08-19.0', provider_timeout_seconds=5,
            footprint_search_radius_meters=45, footprint_max_distance_meters=20,
            footprint_ambiguity_meters=2)

    @patch.dict('os.environ', {'OVERTURE_RELEASE_POLICY':'current'})
    @patch('app.providers._current_overture_release', return_value='2026-09-16.0')
    def test_current_mode_records_actual_release_without_monthly_config_edit(self, _release):
        self.assertEqual(_download_overture_release(self.settings()), '2026-09-16.0')

    @patch.dict('os.environ', {'OVERTURE_RELEASE_POLICY':'pinned'})
    @patch('app.providers._current_overture_release', return_value='2026-09-16.0')
    def test_strict_pinned_mode_retains_mismatch_gate(self, _release):
        with self.assertRaises(UnreliableGeometryError) as caught:
            _download_overture_release(self.settings())
        self.assertEqual(caught.exception.code, 'OVERTURE_RELEASE_MISMATCH')

    @patch.dict('os.environ', {'OVERTURE_RELEASE_POLICY':'invalid'})
    def test_invalid_policy_is_not_silently_accepted(self):
        with self.assertRaises(UnreliableGeometryError):
            _download_overture_release(self.settings())

    @patch.dict('os.environ', {'OVERTURE_RELEASE_POLICY':'current'})
    @patch('app.providers._current_overture_release', side_effect=['2026-09-16.0','2026-10-14.0'])
    @patch('app.providers._run')
    def test_changed_release_during_download_is_not_mislabeled(self, _run, _release):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UnreliableGeometryError) as caught:
                fetch_overture_footprint(-81.9509,26.6211,Path(directory),self.settings())
        self.assertEqual(caught.exception.code,'OVERTURE_RELEASE_CHANGED_DURING_DOWNLOAD')

    @patch.dict('os.environ', {'OVERTURE_RELEASE_POLICY':'current'})
    @patch('app.providers._current_overture_release', return_value='2026-09-16.0')
    def test_download_labels_the_release_actually_used(self, _release):
        with tempfile.TemporaryDirectory() as directory:
            workspace=Path(directory)
            def run(command, **kwargs):
                Path(command[-1]).write_text(json.dumps({'type':'FeatureCollection','features':[
                    {'type':'Feature','properties':{'id':'synthetic-building'},
                     'geometry':mapping(box(-81.9510,26.6210,-81.9508,26.6212))}]}))
            with patch('app.providers._run', side_effect=run):
                result=fetch_overture_footprint(-81.9509,26.6211,workspace,self.settings())
        self.assertEqual(result.overture_release,'2026-09-16.0')
        self.assertEqual(result.overture_id,'synthetic-building')

if __name__=='__main__':
    unittest.main()
