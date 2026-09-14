# Terraform for ctxwindow.uk

Every piece of live GCP infrastructure behind ctxwindow.uk is described
here: the Cloud Run service, both service accounts and their IAM, the
Artifact Registry repository and its cleanup policies, Firestore, the
Secret Manager secret, the GCS state bucket and its access-log bucket,
and the uptime check with its alert policy. Resources were IMPORTED into
state rather than recreated, so a clean plan is additive only.

The Cloudflare Worker that fronts this on ctxwindow.uk is NOT managed
here. It deploys from `cloudflare-proxy/` via wrangler in `deploy.yml`.

## How a change reaches production

Nobody runs `terraform apply` by hand.

1. Open a PR. `.github/workflows/tests.yml`'s `terraform-plan` job runs
   `fmt -check`, `init`, `validate`, `tflint`, `checkov` and `plan`
   against real state, authenticating through Workload Identity
   Federation. There are no static service-account keys anywhere.
2. Merge to `main`. `.github/workflows/deploy.yml` builds and pushes the
   image, runs `plan` then `apply`, smoke-tests `/health`, and rolls back
   to the previously-live image tag if that smoke test fails.

`terraform-plan` is a required status check, so a plan that errors
blocks the merge.

## Running a plan yourself

You need `roles/viewer`-equivalent read access plus the state bucket.
CI is the supported path; this is for debugging.

```sh
cd infrastructure/terraform
terraform init -input=false
terraform plan -input=false -var="image_tag=$(git rev-parse HEAD)"
```

`image_tag` has no default on purpose: a bare local apply must not be
able to silently roll production back to a stale image.

## The one manual step: extending deployTerraformReader

`github-deploy` holds resource-scoped write roles for the things it
manages, plus one custom role, `deployTerraformReader`, carrying only
the READ permissions `terraform plan` needs to refresh resources it does
not hold a broad role on. That design is deliberate. An earlier revision
of `iam.tf` reached for `roles/resourcemanager.projectIamAdmin` to fix a
single read-only refresh error, which would have let `github-deploy`
grant itself Owner on a project that also hosts two unrelated services.

So when a plan fails with a 403, add the exact permission the error
names to `google_project_iam_custom_role.deploy_terraform_reader`. Never
add a predefined role that happens to contain it.

**That change does not take effect on the PR that makes it.** The role
is only written to GCP when the PR merges and `deploy.yml` applies it,
but the plan that needs the permission runs before the merge. Adding a
permission is therefore two steps:

```sh
# 1. commit the permission to iam.tf, then
# 2. someone with project IAM admin mirrors it onto the live role,
#    using the gcp_project value from terraform.tfvars:
gcloud iam roles update deployTerraformReader \
  --project=<gcp_project> \
  --add-permissions=the.exact.permission
```

Then re-run the PR's `terraform-plan`. Once the PR merges, `apply`
reconciles the live role with the file and the two agree again.

Permissions currently in the role, and why each one is there:

| Permission | Refreshes |
|---|---|
| `resourcemanager.projects.getIamPolicy` | the two `google_project_iam_member` bindings |
| `iam.serviceAccounts.get` | both `google_service_account` resources |
| `iam.serviceAccounts.getIamPolicy` | the `actAs` and workload-identity bindings on them |
| `iam.workloadIdentityPools.get`, `.getAttestationRules` | the `google_iam_workload_identity_pool` data source |
| `iam.roles.get` | the custom role reading back its own definition |
| `datastore.databases.getMetadata` | `google_firestore_database` |
| `storage.buckets.get`, `.getIamPolicy` | both buckets and the tfstate binding |
| `artifactregistry.repositories.getIamPolicy` | the Artifact Registry writer binding |
| `secretmanager.secrets.get`, `.getIamPolicy` | the secret and its two accessor bindings |

`datastore.databases.getMetadata` is the subtle one. On a
`FIRESTORE_NATIVE` database, `datastore.databases.get` authorises
beginning and rolling back a transaction, which is a data-plane write,
and it does NOT authorise the Firestore Admin `projects.databases.get`
call that the resource refresh actually makes. `getMetadata` is the
correct permission and it cannot read a single document.

## Known manual state

- **The alert notification channel is likely unverified.** GCP emails a
  confirmation link when an email notification channel is created, and
  an unverified channel silently never fires. Terraform cannot verify it
  for you: open Monitoring, Alerting, Notification channels and confirm
  the address shows as verified.
- **`.terraform.lock.hcl` records only one platform's `h1:` hash**, so
  `terraform init` on a Linux runner adds the Linux one and reports the
  lock file as changed on every CI run. Fix from a machine that can
  reach registry.terraform.io:
  `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`,
  then commit the result. The file also still carries a
  `cloudflare/cloudflare` entry from before the Worker moved to wrangler;
  the same command drops it.
