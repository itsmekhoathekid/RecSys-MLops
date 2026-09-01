# Substrate DurDir before artifacts -- 2026-08-28

This directory retains the resolved configuration and normalized result table
for the before-optimization DurDir run on production GKE.

Files:

- `run-config.yaml`: resolved workload, image, cluster, and load-generator
  inputs used by the measured job.
- `workloads-applied.yaml`: the benchmark WorkerPool and ActorTemplate
  applied to the cluster.
- `data-stats.csv`: the before-optimization benchmark result.

The Kubernetes jobs printed Locust CSVs to stdout. The jobs and benchmark
namespace were deleted during cleanup, and the cluster did not retain their
stdout in Cloud Logging. The CSV here is therefore a normalized copy of the
captured Locust summary values, not an original Locust-generated file. This
distinction prevents the retained evidence from being represented as raw data.

The authoritative narrative and interpretation are in
[`../../benchmark_ha.md`](../../benchmark_ha.md).
