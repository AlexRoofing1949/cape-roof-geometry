"""Release guards tested with simulated gcloud; never claims a live deployment."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'deploy/cloud_run_release.py'
spec=importlib.util.spec_from_file_location('cloud_run_release',path)
r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
SHA='a'*40
BASE='us-central1-docker.pkg.dev/'+r.PROJECT+'/existing/geometry'
DIGEST=BASE+'@sha256:'+'b'*64

def service():
    return {'metadata':{'generation':4},'spec':{'template':{'spec':{
        'serviceAccountName':'existing-runtime', 'containerConcurrency':1,
        'containers':[{'image':BASE+':old','env':[{'name':'AUTH_BEARER_TOKEN','valueFrom':{'secretKeyRef':{'name':'existing-secret','key':'latest'}}},{'name':'SERVICE_COMMIT','value':'old'}]}]
    }}},'status':{'latestReadyRevisionName':'old-r1','latestCreatedRevisionName':'old-r1','traffic':[{'revisionName':'old-r1','percent':100}]}}

def candidate():
    s=service(); c=s['spec']['template']['spec']['containers'][0]
    c['image']=DIGEST; c['env'][1]['value']=SHA
    s['status'].update(latestReadyRevisionName='new-r2',latestCreatedRevisionName='new-r2')
    s['status']['traffic'].append({'revisionName':'new-r2','tag':'release-'+SHA[:12],'percent':0,'url':'https://candidate.example.invalid'})
    return s

class ReleaseTests(unittest.TestCase):
    def test_same_project_image_destination(self):
        self.assertEqual(r.image_repository(BASE+'@sha256:abc'),BASE)
    def test_foreign_project_rejected(self):
        with self.assertRaises(r.ReleaseError): r.image_repository('gcr.io/not-this-project/geometry:latest')
    def test_unknown_registry_rejected(self):
        with self.assertRaises(r.ReleaseError): r.image_repository('docker.io/example/geometry:latest')
    def test_secret_configuration_preserved(self):
        self.assertEqual(r.stable_config(service()),r.stable_config(candidate()))
    def test_changed_secret_detected(self):
        s=candidate(); s['spec']['template']['spec']['containers'][0]['env'][0]['valueFrom']['secretKeyRef']['name']='other'
        self.assertNotEqual(r.stable_config(service()),r.stable_config(s))
    def test_no_traffic_candidate_preserves_production(self):
        self.assertEqual(r.traffic_map(service()),r.traffic_map(candidate()))
    def test_partial_traffic_snapshot_rejected(self):
        s=service(); s['status']['traffic'][0]['percent']=80
        with self.assertRaises(r.ReleaseError): r.traffic_map(s)
    def test_multicontainer_requires_review(self):
        s=service(); s['spec']['template']['spec']['containers'].append({'image':'sidecar'})
        with self.assertRaises(r.ReleaseError): r.stable_config(s)
    def test_missing_gcloud_stops_before_access(self):
        with patch.object(r.shutil,'which',return_value=None),patch.object(r,'gcloud') as g:
            with self.assertRaisesRegex(r.ReleaseError,'GOOGLE_AUTH_REQUIRED'): r.stage(True)
            g.assert_not_called()
    def test_preflight_does_not_build(self):
        with patch.object(r.shutil,'which',return_value='/gcloud'),patch.object(r,'gcloud',return_value=json.dumps(service())) as g,patch.object(r,'command',side_effect=[SHA,'']):
            self.assertEqual(r.stage()['state'],'PREFLIGHT_PASSED'); self.assertEqual(g.call_count,1)
    def test_wrong_candidate_commit_rejected(self):
        with self.assertRaises(r.ReleaseError): r.staged_revision(candidate(),'c'*40,DIGEST)
    def test_build_includes_all_three_test_groups(self):
        config=r.build_config(BASE+':test'); text=json.dumps(config)
        for v in ('unittest','native_geometry_smoke.py','test_apps_script_repair.cjs'): self.assertIn(v,text)
        self.assertEqual(config['images'],[BASE+':test'])
    def test_stage_only_no_traffic_and_private_backup(self):
        with tempfile.TemporaryDirectory() as d:
            build={'status':'SUCCESS','id':'fake-build','results':{'images':[{'name':BASE+':release-'+SHA,'digest':'sha256:'+'b'*64}]}}
            with patch.object(r.shutil,'which',return_value='/gcloud'),patch.object(r,'gcloud',side_effect=[json.dumps(service()),json.dumps(build),json.dumps(service()),'{}',json.dumps(candidate())]) as g,patch.object(r,'command',side_effect=[SHA,'',b'test-archive']):
                result=r.stage(True,Path(d)/'private')
                self.assertEqual(result['state'],'STAGED_NOT_PROMOTED')
                self.assertIn('--no-traffic',g.call_args_list[3].args)
                self.assertFalse(result['productionTrafficChanged'])
                self.assertEqual((Path(d)/'private/service-before.private.json').stat().st_mode & 0o777,0o600)
    def test_build_failure_stops_deploy(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(r.shutil,'which',return_value='/gcloud'),patch.object(r,'gcloud',side_effect=[json.dumps(service()),json.dumps({'status':'FAILURE'})]) as g,patch.object(r,'command',side_effect=[SHA,'',b'test-archive']):
                with self.assertRaisesRegex(r.ReleaseError,'nothing deployed'): r.stage(True,Path(d)/'private')
                self.assertEqual(g.call_count,2)

if __name__=='__main__': unittest.main()
