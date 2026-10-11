"""Stage a tested immutable revision of the EXISTING CapeRoof service.

Run from a clean checkout in an authenticated Google Cloud environment.
Never changes IAM, DNS, secrets, pricing switches, or production traffic.
Only tracked files are sent to Cloud Build. Full service snapshots stay private.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = 'wise-dispatcher-506222-q4'
REGION = 'us-central1'
SERVICE = 'cape-roof-geometry'
SOURCE_URL = 'https://github.com/AlexRoofing1949/cape-roof-geometry'
ROOT = Path(__file__).resolve().parents[1]

class ReleaseError(RuntimeError):
    pass

def command(args, *, cwd=ROOT, binary=False, timeout=1800):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=not binary,
                           timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReleaseError('Required command unavailable or timed out: ' + args[0]) from e
    if r.returncode:
        # Commands can include private configuration in stderr. Do not publish it.
        raise ReleaseError(f'{args[0]} command failed with exit {r.returncode}; inspect its authenticated console privately.')
    return r.stdout

def gcloud(*args):
    return command(['gcloud', *args, '--project=' + PROJECT, '--format=json', '--quiet'])

def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump(value, f, indent=2)

def image_repository(image):
    base = image.split('@', 1)[0]
    head, tail = base.rsplit('/', 1)
    base = head + '/' + tail.split(':', 1)[0]
    parts = base.split('/')
    allowed = (parts[0] in ('gcr.io','us.gcr.io','eu.gcr.io','asia.gcr.io') or
               bool(re.fullmatch(r'[a-z0-9-]+-docker\.pkg\.dev', parts[0])))
    if not allowed or len(parts) < 3 or parts[1] != PROJECT or not re.fullmatch(r'[a-zA-Z0-9/_.-]+', base):
        raise ReleaseError('Current image is not in the expected project registry; refusing to invent a destination.')
    return base

def stable_config(service):
    result = copy.deepcopy(service['spec']['template']['spec'])
    containers = result.get('containers', [])
    if len(containers) != 1:
        raise ReleaseError('Expected the existing single-container service; review multi-container configuration manually.')
    container = containers[0]
    container.pop('image', None)
    container['env'] = sorted([x for x in container.get('env', [])
                              if x['name'] not in ('SERVICE_COMMIT','SERVICE_SOURCE_URL')], key=lambda x:x['name'])
    return result

def traffic_map(service):
    result = {}
    for t in service.get('status', {}).get('traffic', []):
        if t.get('percent', 0):
            revision = t.get('revisionName')
            if not revision:
                raise ReleaseError('Production traffic is not resolved to immutable revision names.')
            result[revision] = result.get(revision, 0) + int(t['percent'])
    if sum(result.values()) != 100:
        raise ReleaseError('Cannot establish a complete production traffic snapshot.')
    return result

def build_config(image):
    mount = ['--rm','-v','/workspace:/checks:ro','-e','PYTHONPATH=/srv/cape-roof-geometry:/checks']
    return {
        'steps': [
            {'name':'gcr.io/cloud-builders/docker','args':['build','-t',image,'.']},
            {'name':'gcr.io/cloud-builders/docker','args':['run',*mount,image,'python','-m','unittest','discover','-s','/checks/tests','-v']},
            {'name':'gcr.io/cloud-builders/docker','args':['run',*mount,image,'python','/checks/tools/native_geometry_smoke.py']},
            {'name':'node:22','entrypoint':'node','args':['--test','tests/test_apps_script_repair.cjs']}
        ],
        'images':[image], 'timeout':'1200s'
    }

def staged_revision(service, commit, digest):
    candidate = service.get('status', {}).get('latestCreatedRevisionName')
    if not candidate or candidate != service['status'].get('latestReadyRevisionName'):
        raise ReleaseError('Candidate is not ready; production traffic has not been promoted.')
    container = service['spec']['template']['spec']['containers'][0]
    env = {x['name']:x.get('value') for x in container.get('env', [])}
    if env.get('SERVICE_COMMIT') != commit or container.get('image') != digest:
        raise ReleaseError('Candidate image/provenance differs from the tested source.')
    return candidate

def stage(apply=False, state_dir=None):
    if not shutil.which('gcloud'):
        raise ReleaseError('GOOGLE_AUTH_REQUIRED: run in an authenticated Google Cloud Shell; gcloud is unavailable here.')
    # Resolve the already existing service before submitting any billable build.
    before = json.loads(gcloud('run','services','describe',SERVICE,'--region='+REGION))
    stable = stable_config(before)
    traffic = traffic_map(before)
    current_image = before['spec']['template']['spec']['containers'][0]['image']
    repository = image_repository(current_image)
    commit = command(['git','rev-parse','HEAD']).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ReleaseError('A full immutable source commit is required.')
    if command(['git','status','--porcelain']).strip():
        raise ReleaseError('Use a clean checkout; no uncommitted files may be deployed.')
    result = {'project':PROJECT,'region':REGION,'service':SERVICE,'sourceCommit':commit,
              'productionTrafficChanged':False,'pricingChanged':False,'state':'PREFLIGHT_PASSED'}
    if not apply:
        return result
    folder = Path(state_dir or (Path.home()/'.caperoof-releases'/commit)).resolve()
    if folder == ROOT or ROOT in folder.parents:
        raise ReleaseError('Private deployment state must be outside the repository.')
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_json(folder/'service-before.private.json', before)
    image = repository + ':release-' + commit
    archive = command(['git','archive','--format=tar.gz',commit], binary=True)
    with tempfile.TemporaryDirectory(prefix='caperoof-build-') as temp:
        source = Path(temp)/'source.tgz'; source.write_bytes(archive)
        config = Path(temp)/'cloudbuild.json'; config.write_text(json.dumps(build_config(image)))
        build = json.loads(gcloud('builds','submit',str(source),'--config='+str(config),'--suppress-logs','--timeout=1200s'))
    if isinstance(build,list):
        if len(build) != 1: raise ReleaseError('Unexpected Cloud Build response.')
        build=build[0]
    if build.get('status') != 'SUCCESS':
        raise ReleaseError('Cloud Build or required tests failed; nothing deployed.')
    matches = [x for x in build.get('results',{}).get('images',[]) if x.get('name') == image]
    if len(matches) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}',matches[0].get('digest','')):
        raise ReleaseError('Missing immutable digest for the tested image.')
    digest = repository + '@' + matches[0]['digest']
    fresh = json.loads(gcloud('run','services','describe',SERVICE,'--region='+REGION))
    if fresh.get('metadata',{}).get('generation') != before.get('metadata',{}).get('generation') or traffic_map(fresh) != traffic:
        raise ReleaseError('Service changed during build; stop and reconcile before deployment.')
    gcloud('run','deploy',SERVICE,'--region='+REGION,'--image='+digest,
           '--no-traffic','--tag=release-'+commit[:12],'--revision-suffix=r'+commit[:12],
           '--update-env-vars=SERVICE_COMMIT='+commit+',SERVICE_SOURCE_URL='+SOURCE_URL)
    after = json.loads(gcloud('run','services','describe',SERVICE,'--region='+REGION))
    if stable_config(after) != stable or traffic_map(after) != traffic:
        raise ReleaseError('Unexpected configuration or traffic drift; inspect private before snapshot. No promotion requested.')
    revision = staged_revision(after,commit,digest)
    url = next((t.get('url') for t in after.get('status',{}).get('traffic',[]) if t.get('tag') == 'release-'+commit[:12]),None)
    result.update(state='STAGED_NOT_PROMOTED',revision=revision,imageDigest=digest,candidateUrl=url,buildId=build.get('id'))
    private_json(folder/'staged-release.json',result)
    result['privateStateDirectory']=str(folder)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',action='store_true',help='Build/test and deploy a no-traffic candidate. Default is read-only preflight.')
    p.add_argument('--state-dir',help='New private directory outside the repository.')
    args=p.parse_args()
    try:
        print(json.dumps(stage(args.stage,args.state_dir),indent=2))
    except (ReleaseError,ValueError,KeyError,OSError) as e:
        print('RELEASE_STOPPED: '+str(e),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
