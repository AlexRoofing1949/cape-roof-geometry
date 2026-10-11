# Connect Google for CapeRoof releases

## This is an authentication prerequisite, not an operational estimator

Owner authorization has been given. The current chat session has no Google Cloud credentials and no Apps Script project-execution connector. This setup must run in the owner's signed-in Google Cloud Shell; saying that access is granted in a chat does not create Google credentials.

The setup inspects the existing project, service, runtime identity, image repository, source-staging bucket, service accounts and workload identity providers before making changes. It does not recreate the project, service, pricebook or application. It creates dedicated build/deploy identities and a narrowly scoped GitHub identity provider only when its inventory finds them absent. Existing conflicting identities stop the setup rather than being overwritten. The script never reads application secrets or creates long-lived private keys.

## Run in Google Cloud Shell as alex@caperoof.com

From a clean checkout of the current main branch:

```sh
python3 deploy/connect_google.py
python3 deploy/connect_google.py --apply
```

The first command only prints a plan. The second makes real IAM changes and verifies their readback. Google will require an authenticated owner session and sufficient IAM permissions. Successful output is `GOOGLE_IAM_APPLIED_OIDC_NOT_YET_TESTED`, NOT deployment completion.

The output `connection` object contains four non-secret identifiers. They can be supplied back to the assistant or added as GitHub Actions repository variables. Do not share tokens, private keys, passwords or `.clasprc.json`.

With an already authenticated GitHub CLI, the alternative below applies the same setup and publishes only the four repository variables, verifying repository numeric identity first:

```sh
python3 deploy/connect_google.py --apply --publish-github
```

Then trigger the existing staging workflow:

```sh
gh workflow run cloud-run-stage.yml --repo AlexRoofing1949/cape-roof-geometry --ref main
```

If GitHub CLI is unavailable or not signed in, the Google IAM setup can still complete without `--publish-github`. Return only the `connection` object. The assistant can incorporate those verified identifiers into the repository workflow through the connected GitHub tool.

## Permissions

The deployment identity can update only the existing Cloud Run service, use its existing runtime identity and the dedicated builder, read the existing artifact repository, submit project builds and upload sources to the verified existing staging bucket. The builder can read those sources, write artifacts to that repository and write build logs. No primitive Owner or Editor role, project-wide storage administrator, account keys, public invoker access or Secret Manager secret access is granted. Identity federation is limited to the verified numeric owner and repository IDs, the main branch, this release workflow, and push/workflow-dispatch events.

The setup does NOT grant Apps Script OAuth scopes. The original Apps Script project must still be accessed through owner-authorized Apps Script APIs or the editor; its sources, triggers and existing deployed web app must be preserved. Do not replace the original project with the public geometry repository. No pricing activation or customer email occurs here.

## Actual deployment and acceptance

After authentication is configured, the staging workflow builds and tests the image and deploys with zero production traffic. It preserves the current service configuration and records the old traffic targets. A real property measurement, original Apps Script integration, monetary checks, PDF storage, test-only delivery and owner-account trigger verification must pass before promotion and pricing activation. Historical source data alone and synthetic geometry tests are not end-to-end acceptance evidence.

## Verification scope

The setup and release tests use mocked cloud responses. Passing them does not prove the IAM bindings exist or the service has been deployed. Only the owner-run setup's readbacks and an authenticated workflow provide that evidence.

Official implementation references:
- https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
- https://github.com/google-github-actions/auth
- https://docs.cloud.google.com/run/docs/deploying
- https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit
- https://developers.google.com/apps-script/api/reference/rest/v1/projects/getContent
