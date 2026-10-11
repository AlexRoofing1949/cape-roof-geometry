"""Offline tests only. These do not prove successful Google authentication."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
c=load('connect_google',ROOT/'deploy/connect_google.py')
r=load('release_setup_test',ROOT/'deploy/cloud_run_release.py')

def inventory():
    return dict(projectNumber='123456789',runtimeAccount='existing-runtime@'+c.PROJECT+'.iam.gserviceaccount.com',
        registryLocation='us-central1', registryRepository='existing', sourceBucket=c.PROJECT+'_cloudbuild',
        deployAccount=c.DEPLOY_ID+'@'+c.PROJECT+'.iam.gserviceaccount.com',
        buildAccount=c.BUILD_ID+'@'+c.PROJECT+'.iam.gserviceaccount.com',
        poolName='projects/123456789/locations/global/workloadIdentityPools/'+c.POOL,
        deployExists=False,buildExists=False,poolExists=False,providerExists=False,missingApis=[])

def provider():
    return dict(state='ACTIVE',attributeMapping=c.MAPPING,attributeCondition=c.CONDITION,
                oidc={'issuerUri':'https://token.actions.githubusercontent.com'})

class ConnectionTests(unittest.TestCase):
    def test_no_cloud_tool_makes_zero_cloud_calls(self):
        with patch.object(c.shutil,'which',return_value=None), patch.object(c,'gc') as gc:
            with self.assertRaisesRegex(c.SetupError,'GOOGLE_SIGN_IN_REQUIRED'):c.setup(True)
            gc.assert_not_called()
    def test_no_credentials_are_created_or_read(self):
        text=json.dumps(c.operations(inventory()))
        for forbidden in ['keys', 'secretmanager', 'allUsers', 'roles/owner', 'roles/editor', 'roles/run.admin', 'set-iam-policy']:
            self.assertNotIn(forbidden,text)
    def test_cloud_run_permission_is_service_scoped(self):
        op=next(x for x in c.operations(inventory()) if '--role=roles/run.developer' in x)
        self.assertEqual(op[:4],['run','services','add-iam-policy-binding',c.SERVICE])
    def test_registry_permission_is_repository_scoped(self):
        op=next(x for x in c.operations(inventory()) if '--role=roles/artifactregistry.writer' in x)
        self.assertEqual(op[:4],['artifacts','repositories','add-iam-policy-binding','existing'])
    def test_oidc_is_bound_to_exact_repository_workflow_branch_and_events(self):
        for v in [c.REPO_ID,c.REPO_OWNER_ID,c.WORKFLOW,'refs/heads/main',"['push', 'workflow_dispatch']"]:
            self.assertIn(v,c.CONDITION)
        self.assertNotIn('pull_request',c.CONDITION)
    def test_provider_exact_match_accepted(self):c.validate_provider(provider())
    def test_broader_trust_not_accepted(self):
        p=provider();p['attributeCondition']='true'
        with self.assertRaises(c.SetupError):c.validate_provider(p)
    def test_other_issuer_not_accepted(self):
        p=provider();p['oidc']['issuerUri']='https://example.invalid'
        with self.assertRaises(c.SetupError):c.validate_provider(p)
    def test_disabled_provider_not_accepted(self):
        p=provider();p['disabled']=True
        with self.assertRaises(c.SetupError):c.validate_provider(p)
    def test_existing_resources_not_recreated(self):
        i=inventory()
        for k in ['deployExists','buildExists','poolExists','providerExists']:i[k]=True
        self.assertFalse(any('create' in x or 'create-oidc' in x for x in c.operations(i)))
    def test_collision_not_reused(self):
        email=c.DEPLOY_ID+'@'+c.PROJECT+'.iam.gserviceaccount.com'
        with self.assertRaises(c.SetupError):c.managed_account([{'email':email,'description':'other'}],c.DEPLOY_ID)
    def test_managed_account_reused(self):
        email=c.DEPLOY_ID+'@'+c.PROJECT+'.iam.gserviceaccount.com'
        self.assertEqual(c.managed_account([{'email':email,'description':c.MARKER}],c.DEPLOY_ID),(email,True))
    def test_foreign_registry_rejected(self):
        with self.assertRaises(c.SetupError):c.registry_resource('us-central1-docker.pkg.dev/other/repo/image')
    def test_valid_registry_parsed(self):
        self.assertEqual(c.registry_resource('us-central1-docker.pkg.dev/'+c.PROJECT+'/existing/image:tag'),('us-central1','existing'))
    def test_default_compute_identity_not_blindly_reused(self):
        with self.assertRaises(c.SetupError):c.account_email('123-compute@developer.gserviceaccount.com')
    def test_plan_does_not_mutate(self):
        with patch.object(c,'inventory',return_value=inventory()),patch.object(c,'gc') as gc:
            result=c.setup(False)
            self.assertEqual(result['state'],'PLAN_ONLY');gc.assert_not_called()
    def test_partial_failure_identifies_partial_state(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c.Path,'home',return_value=Path(d)),patch.object(c,'inventory',return_value=inventory()),patch.object(c,'gc',side_effect=[None,c.SetupError('denied')]):
            with self.assertRaisesRegex(c.SetupError,'after 1 successful operations'):c.setup(True)
            self.assertEqual(len(list(Path(d).rglob('applied-*.json'))),1)
    def test_private_receipt_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'receipt.json';c.write_private(p,{'ok':True})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
    def test_unconfigured_build_preserves_existing_behavior(self):
        with patch.dict(os.environ,{},clear=True):self.assertNotIn('serviceAccount',r.build_config('test'))
    def test_configured_build_uses_dedicated_identity(self):
        with patch.dict(os.environ,c.public_connection(inventory()),clear=True):
            config=r.build_config('test')
            self.assertIn(c.BUILD_ID,config['serviceAccount'])
            self.assertEqual(config['options']['logging'],'CLOUD_LOGGING_ONLY')
    def test_missing_or_foreign_build_account_rejected(self):
        with patch.dict(os.environ,{'CAPE_ROOF_BUILD_STAGING_BUCKET':c.PROJECT+'_cloudbuild'},clear=True):
            with self.assertRaises(r.ReleaseError):r.build_identity_config()
    def test_wrong_staging_bucket_rejected(self):
        env=c.public_connection(inventory());env['CAPE_ROOF_BUILD_STAGING_BUCKET']='other'
        with patch.dict(os.environ,env,clear=True):
            with self.assertRaises(r.ReleaseError):r.build_identity_config()

if __name__=='__main__':unittest.main()
