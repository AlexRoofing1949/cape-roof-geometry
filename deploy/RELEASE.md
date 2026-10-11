# CapeRoof production release

Target: existing project `wise-dispatcher-506222-q4`, service `cape-roof-geometry`, region `us-central1`.
The external `geometry.caperoof.com` load balancer points to this region. Do not create a duplicate service elsewhere.

## Current authorization boundary

The owner requested implementation and deployment on October 11, 2026. Measurement PR #2 was merged. The deployment access job found no configured workload identity provider or deployment service account under the supported repository/job settings. This is an authentication prerequisite, not a successful deployment. No secret values were retrieved.

## Authenticated staging

Run from a clean checkout in an already authenticated Google Cloud Shell:

```sh
python3 deploy/cloud_run_release.py
python3 deploy/cloud_run_release.py --stage
```

The first command is read-only. The second submits a billable Cloud Build, runs the full Python regression tests, native PDAL/Roofer/Open3D smoke test and JavaScript helper tests, verifies the resulting immutable image digest, then deploys a tagged NO-TRAFFIC candidate. It discovers the existing registry from the current service, retains its configuration and secret references, and records a private rollback snapshot outside Git. It does not change IAM, DNS, pricing flags or customer traffic. No source edits or untracked files are submitted to Cloud Build.

GitHub Actions can perform the same operation through `Stage CapeRoof Cloud Run release` after the existing Google workload identity provider is configured in `GCP_WORKLOAD_IDENTITY_PROVIDER` and, when impersonation is used, the existing deployment identity in `GCP_DEPLOY_SERVICE_ACCOUNT`. These are identifiers, not private keys. The workflow also accepts the documented aliases in its source. This release does not create IAM bindings, service accounts, providers, billing relationships, or long-lived JSON keys. Missing authentication intentionally stops the job.

A clean CLI preflight requires read access to the existing service. Staging additionally requires the project's existing build, registry push and Cloud Run revision-deployment permissions and permission to use the existing runtime service identity. Preserve least-privilege access. Do not give a public user Cloud Run admin access or expose application bearer credentials.

## Apps Script is a separate authenticated deployment

Existing script: `1FdhVNAx5_LCmWF-4APiGOGi7tSeo9QwT_iVG0ahnSaeP3Ar_DjBvOyHQ`.
Existing workbook: `1cFKZPsJ4rIWIGx9PiacuO7yMTK8Fgr-0wNCk0AkZXHw`.

The Drive connector's earlier media fetch returned 403 and it does not provide the Apps Script project-content or execution APIs. Do not infer that changing Drive sharing or making the project public fixes this. Use the owner's authorized Apps Script editor/API to export and inspect the ORIGINAL complete project. Add `automation/google-apps-script/CapeRoofRepair20261011.gs` without deleting its existing modules. Connect its quantity helper to the real accepted-geometry success path, preserving the original pricebook selection, monetary calculation, PDF, queue, retry and email code. Run the pricebook audit, repair the intended owner's triggers, then version and update the EXISTING web-app deployment.

The helper alone is not an integrated replacement for the private original application. Do not blindly replace the project with the public repository. Do not guess original function contracts, private prices, missing Flat-Rock rates, fee bases or tax treatment.

## Promotion acceptance

NO-TRAFFIC staging is not public release and public health is not estimate acceptance. Before moving traffic, check candidate `/healthz` against the exact staged commit; verify unauthenticated geometry requests are rejected; exercise an authenticated real-property measurement; compare it with a verified reference; finish the original Apps Script integration; and trace a designated test submission through quantities, checked monetary totals, PDF storage and test-only delivery. Confirm deduplication and bounded retries. Keep automatic pricing disabled until these gates pass. Missing Flat-Rock profiles must remain unavailable rather than inherit invented rates.

The staging result and private before-snapshot provide the precise candidate and rollback revision IDs. An operator may promote only the accepted candidate with `gcloud run services update-traffic`, using those observed revision IDs, never a guessed revision or an unverified latest revision. The release utility deliberately does not make that promotion decision.

## Verification scope

`python -m unittest discover -s tests -p 'test_cloud_run_release.py' -v` tests the release guards with mocked Google responses, NOT with live Google authentication. The existing native geometry test uses the actual software on synthetic data, NOT a customer accuracy claim.

Official references:
- https://docs.cloud.google.com/run/docs/deploying
- https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit
- https://developers.google.com/apps-script/api/reference/rest/v1/projects/getContent
- https://developers.google.com/apps-script/api/reference/rest/v1/projects/updateContent
- https://github.com/google-github-actions/auth
