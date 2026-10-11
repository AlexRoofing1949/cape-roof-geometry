"""Owner-run keyless setup for the existing CapeRoof deployment only.

Default: inspect and print a non-secret plan. --apply: create missing dedicated
identities and add narrowly scoped bindings. No keys, application secrets,
pricing switches, live traffic, or original Apps Script content are changed.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = 'wise-dispatcher-506222-q4'
REGION = 'us-central1'
SERVICE = 'cape-roof-geometry'
OWNER = 'alex@caperoof.com'
REPO = 'AlexRoofing1949/cape-roof-geometry'
REPO_ID = '1354013984'
REPO_OWNER_ID = '319120498'
POOL = 'caperoof-release'
PROVIDER = 'github-main'
DEPLOY_ID = 'caperoof-release-deploy'
BUILD_ID = 'caperoof-release-build'
MARKER = 'CapeRoof dedicated keyless release; managed by deploy/connect_google.py'
WORKFLOW = REPO + '/.github/workflows/cloud-run-stage.yml@refs/heads/main'
MAPPING = {'google.subject':'assertion.sub', 'attribute.repository_id':'assertion.repository_id',
           'attribute.repository_owner_id':'assertion.repository_owner_id',
           'attribute.ref':'assertion.ref', 'attribute.workflow_ref':'assertion.workflow_ref',
           'attribute.event_name':'assertion.event_name'}
CONDITION = (f"assertion.repository_id == '{REPO_ID}' && assertion.repository_owner_id == '{REPO_OWNER_ID}'"
             f" && assertion.ref == 'refs/heads/main' && assertion.workflow_ref == '{WORKFLOW}'"
             " && assertion.event_name in ['push', 'workflow_dispatch']")
APIS = ['iam.googleapis.com', 'iamcredentials.googleapis.com', 'sts.googleapis.com',
        'cloudbuild.googleapis.com', 'run.googleapis.com', 'artifactregistry.googleapis.com']

class SetupError(RuntimeError):
    pass


def run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SetupError('Command unavailable or timed out: ' + args[0]) from e
    if result.returncode:
        # Provider errors may contain private configuration. Do not echo them.
        raise SetupError(f'{args[0]} {" ".join(args[1:3])} failed (exit {result.returncode}); inspect the authenticated console.')
    return result.stdout


def gc(*args: str):
    result = run(['gcloud', *args, '--project='+PROJECT, '--format=json', '--quiet'])
    return json.loads(result) if result.strip() else None


def account_email(identifier: str) -> str:
    email = identifier.rsplit('/', 1)[-1]
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@'+re.escape(PROJECT)+r'\.iam\.gserviceaccount\.com', email):
        raise SetupError('Expected a dedicated runtime service account in this project; review before granting actAs.')
    return email


def registry_resource(image: str) -> tuple[str, str]:
    parts = image.split('@', 1)[0].split('/')
    match = re.fullmatch(r'([a-z0-9-]+)-docker\.pkg\.dev', parts[0])
    if not match or len(parts) < 4 or parts[1] != PROJECT or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*', parts[2]):
        raise SetupError('The current image must identify an existing same-project Artifact Registry repository; legacy or external registries need review.')
    return match.group(1), parts[2]


def validate_provider(provider: dict) -> None:
    if (provider.get('state') != 'ACTIVE' or provider.get('disabled', False)
        or provider.get('oidc', {}).get('issuerUri') != 'https://token.actions.githubusercontent.com'
        or provider.get('attributeMapping') != MAPPING
        or provider.get('attributeCondition') != CONDITION):
        raise SetupError('An existing provider has different trust rules; refusing to overwrite or broaden it.')


def managed_account(accounts: list, name: str):
    email = name+'@'+PROJECT+'.iam.gserviceaccount.com'
    matches = [a for a in accounts if a.get('email') == email]
    if len(matches) > 1:
        raise SetupError('Duplicate identity metadata.')
    if matches and (matches[0].get('description') != MARKER or matches[0].get('disabled', False)):
        raise SetupError('A same-named service account is not this setup identity; refusing to reuse it.')
    return email, bool(matches)


def inventory() -> dict:
    if not shutil.which('gcloud'):
        raise SetupError('GOOGLE_SIGN_IN_REQUIRED: run this in your signed-in Google Cloud Shell. No Google changes were made.')
    active = gc('auth', 'list', '--filter=status:ACTIVE')
    if len(active or []) != 1 or active[0].get('account', '').lower() != OWNER:
        raise SetupError('Use the approved Google owner account '+OWNER+' in Cloud Shell.')
    project = gc('projects', 'describe', PROJECT)
    number = str(project.get('projectNumber', ''))
    if project.get('projectId') != PROJECT or project.get('lifecycleState') != 'ACTIVE' or not number.isdigit():
        raise SetupError('Unexpected project identity or status.')
    service = gc('run', 'services', 'describe', SERVICE, '--region='+REGION)
    spec = service['spec']['template']['spec']
    if service.get('metadata', {}).get('name') != SERVICE or len(spec.get('containers', [])) != 1:
        raise SetupError('Unexpected production service layout; no duplicate service will be created.')
    runtime = account_email(spec.get('serviceAccountName', ''))
    location, repository = registry_resource(spec['containers'][0]['image'])
    registry = gc('artifacts', 'repositories', 'describe', repository, '--location='+location)
    expected_registry = f'projects/{PROJECT}/locations/{location}/repositories/{repository}'
    if registry.get('name') not in [expected_registry, expected_registry.replace('/'+PROJECT+'/', '/'+number+'/')]:
        raise SetupError('Registry identity mismatch.')
    # Inspect the documented default staging bucket; a failed read never means create it.
    bucket_name = PROJECT+'_cloudbuild'
    bucket = gc('storage', 'buckets', 'describe', 'gs://'+bucket_name)
    observed = bucket.get('name', '').removeprefix('gs://').rstrip('/')
    if observed != bucket_name or str(bucket.get('project_number', bucket.get('projectNumber', ''))) != number:
        raise SetupError('Existing source-staging bucket is not verified in this project. No new bucket will be created.')
    accounts = gc('iam', 'service-accounts', 'list') or []
    deploy_email, deploy_exists = managed_account(accounts, DEPLOY_ID)
    build_email, build_exists = managed_account(accounts, BUILD_ID)
    pools = gc('iam', 'workload-identity-pools', 'list', '--location=global') or []
    pool_name = f'projects/{number}/locations/global/workloadIdentityPools/{POOL}'
    pool_exists = False
    provider_exists = False
    for pool in pools:
        if pool.get('state') != 'ACTIVE':
            if pool.get('name') == pool_name:
                raise SetupError('Dedicated workload pool is not active; do not recreate it.')
            continue
        pool_id = pool['name'].rsplit('/', 1)[-1]
        providers = gc('iam', 'workload-identity-pools', 'providers', 'list', '--location=global', '--workload-identity-pool='+pool_id) or []
        if pool['name'] == pool_name:
            if pool.get('description') != MARKER or pool.get('disabled', False):
                raise SetupError('Same-named workload pool belongs to a different setup.')
            pool_exists = True
        for provider in providers:
            target = provider.get('name') == pool_name+'/providers/'+PROVIDER
            if target:
                validate_provider(provider)
                provider_exists = True
            elif (provider.get('state') == 'ACTIVE'
                  and provider.get('oidc', {}).get('issuerUri') == 'https://token.actions.githubusercontent.com'
                  and any(v in provider.get('attributeCondition', '') for v in (REPO_ID, REPO, REPO_OWNER_ID))):
                raise SetupError('Another GitHub provider may already serve CapeRoof. Review it rather than creating duplicate trust.')
    enabled = {a.get('config', {}).get('name') for a in (gc('services', 'list', '--enabled') or [])}
    return dict(projectNumber=number, runtimeAccount=runtime, registryLocation=location,
                registryRepository=repository, sourceBucket=bucket_name,
                deployAccount=deploy_email, buildAccount=build_email, poolName=pool_name,
                deployExists=deploy_exists, buildExists=build_exists, poolExists=pool_exists,
                providerExists=provider_exists, missingApis=[a for a in APIS if a not in enabled])


def operations(i: dict) -> list[list[str]]:
    ops = []
    if i['missingApis']:
        ops.append(['services', 'enable', *i['missingApis']])
    for key, name in [('deployExists', DEPLOY_ID), ('buildExists', BUILD_ID)]:
        if not i[key]:
            ops.append(['iam', 'service-accounts', 'create', name, '--description='+MARKER, '--display-name='+name])
    if not i['poolExists']:
        ops.append(['iam', 'workload-identity-pools', 'create', POOL, '--location=global', '--description='+MARKER, '--display-name=CapeRoof release'])
    if not i['providerExists']:
        ops.append(['iam', 'workload-identity-pools', 'providers', 'create-oidc', PROVIDER,
                    '--location=global', '--workload-identity-pool='+POOL,
                    '--issuer-uri=https://token.actions.githubusercontent.com',
                    '--attribute-mapping='+','.join(k+'='+v for k,v in MAPPING.items()),
                    '--attribute-condition='+CONDITION])
    deploy = 'serviceAccount:'+i['deployAccount']
    build = 'serviceAccount:'+i['buildAccount']
    principal = 'principalSet://iam.googleapis.com/'+i['poolName']+'/attribute.repository_id/'+REPO_ID
    def bind(prefix, member, role):
        ops.append([*prefix, '--member='+member, '--role='+role, '--condition=None'])
    bind(['iam','service-accounts','add-iam-policy-binding',i['deployAccount']], principal, 'roles/iam.workloadIdentityUser')
    bind(['run','services','add-iam-policy-binding',SERVICE,'--region='+REGION], deploy, 'roles/run.developer')
    for role in ['roles/cloudbuild.builds.editor', 'roles/serviceusage.serviceUsageConsumer']:
        bind(['projects','add-iam-policy-binding',PROJECT], deploy, role)
    bind(['projects','add-iam-policy-binding',PROJECT], build, 'roles/logging.logWriter')
    for identity in [i['runtimeAccount'], i['buildAccount']]:
        bind(['iam','service-accounts','add-iam-policy-binding',identity], deploy, 'roles/iam.serviceAccountUser')
    registry = ['artifacts','repositories','add-iam-policy-binding',i['registryRepository'],'--location='+i['registryLocation']]
    bind(registry, deploy, 'roles/artifactregistry.reader')
    bind(registry, build, 'roles/artifactregistry.writer')
    bucket = ['storage','buckets','add-iam-policy-binding','gs://'+i['sourceBucket']]
    for member, role in [(deploy,'roles/storage.legacyBucketReader'), (deploy,'roles/storage.objectCreator'), (deploy,'roles/storage.objectViewer'), (build,'roles/storage.objectViewer')]:
        bind(bucket, member, role)
    return ops


def public_connection(i: dict) -> dict:
    return {'GCP_WORKLOAD_IDENTITY_PROVIDER':i['poolName']+'/providers/'+PROVIDER,
            'GCP_DEPLOY_SERVICE_ACCOUNT':i['deployAccount'],
            'CAPE_ROOF_BUILD_SERVICE_ACCOUNT':'projects/'+PROJECT+'/serviceAccounts/'+i['buildAccount'],
            'CAPE_ROOF_BUILD_STAGING_BUCKET':i['sourceBucket']}


def write_private(path: Path, value) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as out:
        json.dump(value, out, indent=2)


def setup(apply: bool = False, publish_github: bool = False) -> dict:
    i = inventory()
    ops = operations(i)
    connection = public_connection(i)
    if not apply:
        return {'state':'PLAN_ONLY', 'connection':connection, 'plannedGoogleCommands':ops,
                'googleResourcesChanged':False, 'liveDeploymentCompleted':False}
    base = Path.home()/'.caperoof-connection'
    base.mkdir(exist_ok=True, mode=0o700)
    folder = Path(tempfile.mkdtemp(prefix='setup-', dir=base))
    os.chmod(folder, 0o700)
    write_private(folder/'plan.json', {'inventory':i, 'commands':ops})
    for index, op in enumerate(ops):
        try:
            gc(*op)
        except SetupError as e:
            raise SetupError(f'SETUP_STOPPED after {index} successful operations. Journal: {folder}. Earlier changes may remain. {e}') from e
        write_private(folder/f'applied-{index:02d}.json', {'command':op, 'completed':True})
    # Re-read every binding so a command's successful return is not the only evidence.
    for op in ops:
        if 'add-iam-policy-binding' not in op:
            continue
        p = op.index('add-iam-policy-binding')
        request = op[:p]+['get-iam-policy']+op[p+1:]
        member = next(x[len('--member='):] for x in op if x.startswith('--member='))
        role = next(x[len('--role='):] for x in op if x.startswith('--role='))
        request = [x for x in request if not x.startswith(('--member=','--role=','--condition='))]
        policy = gc(*request) or {}
        if not any(b.get('role') == role and member in b.get('members', []) and not b.get('condition') for b in policy.get('bindings', [])):
            raise SetupError('A requested binding did not verify. Review journal '+str(folder))
    refreshed = inventory()
    if not all(refreshed[k] for k in ['deployExists','buildExists','poolExists','providerExists']):
        raise SetupError('IAM setup did not verify completely. Review journal '+str(folder))
    write_private(folder/'connection.json', connection)
    result = {'state':'GOOGLE_IAM_APPLIED_OIDC_NOT_YET_TESTED', 'connection':connection,
              'googleResourcesChanged':True, 'liveDeploymentCompleted':False,
              'appsScriptChanged':False, 'journalDirectory':str(folder), 'githubVariablesConfigured':False}
    if publish_github:
        if not shutil.which('gh'):
            raise SetupError('Google IAM is configured but GitHub CLI is absent. Share only connection.json identifiers, not credentials. Journal '+str(folder))
        run(['gh', 'auth', 'status'])
        repo = json.loads(run(['gh','api','repos/'+REPO]))
        if str(repo.get('id')) != REPO_ID or str(repo.get('owner',{}).get('id')) != REPO_OWNER_ID:
            raise SetupError('GitHub repository identity mismatch; no variables published.')
        for key, value in connection.items():
            run(['gh','variable','set',key,'--repo',REPO,'--body',value])
        observed = {x['name']:x['value'] for x in json.loads(run(['gh','variable','list','--repo',REPO,'--json','name,value']))}
        if any(observed.get(k) != v for k,v in connection.items()):
            raise SetupError('GitHub connection variables did not verify.')
        result['githubVariablesConfigured'] = True
    write_private(folder/'receipt.json', result)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--publish-github', action='store_true', help='Requires an already authenticated gh CLI; publishes identifiers only.')
    args = p.parse_args()
    try:
        print(json.dumps(setup(args.apply, args.publish_github), indent=2))
        return 0
    except (SetupError, KeyError, TypeError, ValueError, OSError) as e:
        print('CONNECTION_NOT_COMPLETED: '+str(e), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
