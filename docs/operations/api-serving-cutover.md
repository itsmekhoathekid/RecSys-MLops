# API Serving Cutover and Rollback

## Preconditions

- Production is champion-only: candidate weight `0`, shadow disabled and no active model rollout.
- Both API images exist by immutable digest.
- Contract, Helm render and image-isolation gates pass.
- Model CD is frozen until both new releases pass smoke tests.

## Cutover

1. Check out the last legacy `recsys-serving` chart revision and upgrade it with the
   repository post-renderer. Helm then records `helm.sh/resource-policy: keep` only on
   the six Feature API resources; the legacy Inference resources remain removable:

   ```bash
   helm upgrade recsys-serving "$LEGACY_RECSYS_SERVING_CHART" \
     --namespace kserve-triton-inference \
     --reuse-values \
     --post-renderer "$PWD/ops/migrations/feature_api_keep_post_renderer.py" \
     --atomic
   helm get manifest recsys-serving -n kserve-triton-inference | \
     rg -n 'recsys-online-feature-api|helm.sh/resource-policy'
   ```

   Do not use the current KServe-only chart for this transitional revision.
2. Install `recsys-online-feature-api` in `api-serving` using Helm `--take-ownership`. Confirm release annotations, unchanged Service ClusterIP and a native `/online-features` request.
3. Install `recsys-inference-api` beside the legacy recommendation workload. Confirm `/healthz`, `/ready`, `/version`, `/metrics`, image digest, Feature API access and a Triton-backed `/recommendations` response.
4. Point demo and gateway backends to `recsys-inference-api`.
5. Observe 5xx rate, request latency, feature-fetch errors and Triton errors for 30 minutes.
6. Upgrade `recsys-serving` to the KServe-only chart, then reconcile the Feature release to remove migration annotations.
7. Import the API releases and require a no-op Terraform plan:

```bash
terraform -chdir=infra/terraform/gcp import 'helm_release.recsys_online_feature_api[0]' api-serving/recsys-online-feature-api
terraform -chdir=infra/terraform/gcp import 'helm_release.recsys_inference_api[0]' api-serving/recsys-inference-api
terraform -chdir=infra/terraform/gcp plan
```

8. Re-enable Model CD. Keep the legacy image digest in the registry for at least 24 hours; it is intentionally absent from the build catalog.

## Acceptance checks

```bash
kubectl -n api-serving rollout status deploy/recsys-online-feature-api
kubectl -n api-serving rollout status deploy/recsys-inference-api
kubectl -n api-serving get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
helm list -n api-serving
helm list -n kserve-triton-inference
```

Deploying or rolling back one API must not change the other API's image, ReplicaSet or restart count. Model promotion must not restart Feature pods.

## Rollback

- Before traffic cutover: uninstall only `recsys-inference-api`; traffic remains on legacy.
- After cutover but before KServe-only cleanup: point gateway/demo back to the legacy Service.
- After cleanup: roll back `recsys-inference-api` to its previous image digest. Do not roll back Feature or KServe.
- Roll back Feature through its own Helm history. Do not change the Inference image.
- If Feature ownership transfer fails, stop before traffic cutover and restore the legacy chart revision.
