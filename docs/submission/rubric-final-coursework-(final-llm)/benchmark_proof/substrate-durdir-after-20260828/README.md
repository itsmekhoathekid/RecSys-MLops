# Substrate DurDir after artifacts -- 2026-08-28

This directory retains the applied optimized configuration and measured native
DurDir result from production GKE.

The only benchmark treatment changed from before was the WorkerPool pod
template: the warm worker was pinned to `recsys-mlops-cpu` and reserved one CPU
and one GiB of memory while retaining CPU burst capacity. User count, duration,
file size, snapshot policy, images, resume mode, and wait time were held fixed.

Files:

- `workloads-applied.yaml`: optimized WorkerPool and unchanged Data
  ActorTemplate treatment.
- `job-applied.yaml`: exact Locust and Boomer Job configuration.
- `data-stats.csv`: original Locust CSV emitted by the successful Job and
  captured from pod logs before cleanup.
- `failures.csv`: original Locust failure CSV; it contains only the header
  because the run had no failures.
- `run-metadata.yaml`: Job timing/status and optimized worker placement proof.

The authoritative comparison and references are in
[`../../benchmark_ha.md`](../../benchmark_ha.md).
