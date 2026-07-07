# Docker And Docker Compose

This document covers the rubric rows:

- Docker and Docker Compose are used.
- Dockerfiles are optimized.
- Image-size/optimization proof is documented.

## Runtime Layout

Docker Compose is the local data-platform runtime. GCP proof builds use Cloud Build so image build does not depend on local Docker.

Code reference:

- [infra/docker/docker-compose.dataflow.yml](../../../infra/docker/docker-compose.dataflow.yml): local data platform compose stack.
- [infra/docker/Dockerfile.base-python](../../../infra/docker/Dockerfile.base-python): shared slim Python base image.
- [apps/data-platform/Dockerfile.dataflow-cli](../../../apps/data-platform/Dockerfile.dataflow-cli): dataflow CLI, Airflow task, Feast/materialize/drift runtime.
- [apps/data-platform/Dockerfile.spark](../../../apps/data-platform/Dockerfile.spark): Spark + Iceberg/Hudi runtime.
- [apps/data-platform/Dockerfile.flink](../../../apps/data-platform/Dockerfile.flink): Flink + Kafka/Iceberg/Redis streaming runtime.
- [infra/cloudbuild/recsys-images.yaml](../../../infra/cloudbuild/recsys-images.yaml): GCP Cloud Build image pipeline.

## Optimization Notes

The Dockerfile optimization follows the Docker Build Cloud guidance from <https://docs.docker.com/build-cloud/optimization/>:

- Multi-stage builds: build dependency/tooling layers in a separate stage, then copy only runtime artifacts into the final stage.
- Multi-threaded tools: enable tool-level parallelism where the tool does not use multiple cores by default.

| Image | Optimization used | Why it reduces image/runtime cost |
|---|---|---|
| `recsys-base-python` | `python:3.11-slim`, `--no-install-recommends`, removes `/var/lib/apt/lists` | Avoids full Debian/Python image and removes apt metadata. |
| `recsys-dataflow-cli` | Multi-stage build: dependency stage uses shared `recsys-base-python`; final stage is `python:3.11-slim` with only venv, configs, data-platform source, feature repo, data generator, Docker scripts, Debezium connector config. `uv` uses `UV_CONCURRENT_DOWNLOADS=8` and `UV_CONCURRENT_BUILDS=8`. | Keeps build tools and dependency resolver out of the final runtime image, avoids copying the full repository, and parallelizes dependency resolution/build work. |
| `recsys-data-generator` | Multi-stage build with `uv` concurrency; final image copies only venv, configs, and data-generator source. | Removes build-only base tooling from the final image and avoids shipping unrelated repo files. |
| `recsys-spark` | Multi-stage JAR downloader; Spark/Iceberg/Hudi/S3 JARs are fetched in parallel with `xargs -P ${DOWNLOAD_JOBS}` and copied into the final Spark runtime. Final stage copies only runtime source folders. | Parallel remote downloads reduce build latency; selective copy avoids docs/tests/artifacts in the runtime layer. |
| `recsys-flink` | Multi-stage JAR downloader; Flink Kafka/Iceberg/Hadoop/S3 JARs are fetched in parallel with `xargs -P ${DOWNLOAD_JOBS}` and copied into the final Flink runtime. Final stage copies only runtime source folders. | Parallel remote downloads reduce build latency; final image keeps only Flink runtime files and required project code. |
| `recsys-airflow` | Multi-stage Airflow dependency image; final Airflow image copies only `/home/airflow/.local` provider packages and DAG/runtime folders. | Avoids copying the full repository into the scheduler/webserver image while preserving Airflow provider dependencies. |
| `recsys-api-serving` | Multi-stage Python venv build with `uv` concurrency; final `python:3.11-slim` image copies only venv, API source, Feast repo, and feature-store module. | Keeps dependency build tooling out of serving runtime and narrows the serving attack surface. |
| `recsys-mlops-training` | Multi-stage Python venv build with `uv` concurrency; final `python:3.11-slim` image copies venv plus ML source, data-platform source, configs, Kubeflow package, and feature repo. | Removes base image build tooling from the final training image and avoids full-repo copy. |

## Before/After Measurement

Use these commands to capture the proof before and after Dockerfile optimization. They keep stable `:before` and `:after` tags, record build latency from `/usr/bin/time -p`, and record image size from `docker image inspect`.

Run this shared helper in the same terminal session before the before/after command blocks:

```bash
set -euo pipefail
cd ./RecSys-MLops
mkdir -p .docker-metrics

measure_build() {
  image="$1"
  logfile="$2"
  shift 2
  /usr/bin/time -p docker build --no-cache -t "$image" "$@" . 2>&1 | tee "$logfile"
}

write_image_sizes() {
  output_path="$1"
  shift
  : > "$output_path"
  for image in "$@"; do
    size_bytes="$(docker image inspect "$image" --format '{{.Size}}')"
    awk -v image="$image" -v size_bytes="$size_bytes" \
      'BEGIN { printf "%-36s %.2f MB\n", image, size_bytes / 1024 / 1024 }' \
      | tee -a "$output_path"
  done
}

write_build_latency() {
  output_path="$1"
  shift
  : > "$output_path"
  for logfile in "$@"; do
    image="$(basename "$logfile" -build.log)"
    latency="$(awk '/^real / {print $2 "s"}' "$logfile" | tail -1)"
    printf "%-28s %s\n" "$image" "$latency" | tee -a "$output_path"
  done
}
```

Run this before applying the Dockerfile optimization:

```bash
set -euo pipefail
cd ./RecSys-MLops
mkdir -p .docker-metrics/before

measure_build recsys-base-python:before .docker-metrics/before/base-python-build.log \
  -f infra/docker/Dockerfile.base-python
measure_build recsys-dataflow-cli:before .docker-metrics/before/dataflow-cli-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/data-platform/Dockerfile.dataflow-cli
measure_build recsys-data-generator:before .docker-metrics/before/data-generator-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/data-platform/data-generator/Dockerfile
measure_build recsys-airflow:before .docker-metrics/before/airflow-build.log \
  -f infra/docker/Dockerfile.airflow
measure_build recsys-spark:before .docker-metrics/before/spark-build.log \
  -f apps/data-platform/Dockerfile.spark
measure_build recsys-flink:before .docker-metrics/before/flink-build.log \
  -f apps/data-platform/Dockerfile.flink
measure_build recsys-api-serving:before .docker-metrics/before/api-serving-build.log \
  -f apps/api-serving/Dockerfile
measure_build recsys-mlops-training:before .docker-metrics/before/mlops-training-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/ml-system/Dockerfile.training

write_image_sizes .docker-metrics/before/image-size.txt \
  recsys-base-python:before \
  recsys-dataflow-cli:before \
  recsys-data-generator:before \
  recsys-airflow:before \
  recsys-spark:before \
  recsys-flink:before \
  recsys-api-serving:before \
  recsys-mlops-training:before

write_build_latency .docker-metrics/before/build-latency.txt .docker-metrics/before/*-build.log
```

Run this after applying the Dockerfile optimization:

```bash
set -euo pipefail
cd ./RecSys-MLops
mkdir -p .docker-metrics/after

measure_build recsys-base-python:after .docker-metrics/after/base-python-build.log \
  -f infra/docker/Dockerfile.base-python
measure_build recsys-dataflow-cli:after .docker-metrics/after/dataflow-cli-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/data-platform/Dockerfile.dataflow-cli
measure_build recsys-data-generator:after .docker-metrics/after/data-generator-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/data-platform/data-generator/Dockerfile
measure_build recsys-airflow:after .docker-metrics/after/airflow-build.log \
  -f infra/docker/Dockerfile.airflow
measure_build recsys-spark:after .docker-metrics/after/spark-build.log \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.spark
measure_build recsys-flink:after .docker-metrics/after/flink-build.log \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.flink
measure_build recsys-api-serving:after .docker-metrics/after/api-serving-build.log \
  -f apps/api-serving/Dockerfile
measure_build recsys-mlops-training:after .docker-metrics/after/mlops-training-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/ml-system/Dockerfile.training

write_image_sizes .docker-metrics/after/image-size.txt \
  recsys-base-python:after \
  recsys-dataflow-cli:after \
  recsys-data-generator:after \
  recsys-airflow:after \
  recsys-spark:after \
  recsys-flink:after \
  recsys-api-serving:after \
  recsys-mlops-training:after

write_build_latency .docker-metrics/after/build-latency.txt .docker-metrics/after/*-build.log
```

Generate the before/after proof summary:

```bash
set -euo pipefail
cd ./RecSys-MLops

{
  echo "## Before Optimization"
  echo
  echo "### Build latency"
  sed 's/^/- /' .docker-metrics/before/build-latency.txt
  echo
  echo "### Image size"
  sed 's/^/- /' .docker-metrics/before/image-size.txt
  echo
  echo "## After Optimization"
  echo
  echo "### Build latency"
  sed 's/^/- /' .docker-metrics/after/build-latency.txt
  echo
  echo "### Image size"
  sed 's/^/- /' .docker-metrics/after/image-size.txt
} | tee .docker-metrics/docker-optimization-proof.md
```

After running the summary command, capture the terminal output as screenshots and save them to these paths for the submission:

![Before Docker optimization proof](../../pngs/docker_before_optimization_proof.png)

![After Docker optimization proof](../../pngs/docker_after_optimization_proof.png)

Use this table for the write-up after running the commands:

| Image | Before build latency (`real`) | After build latency (`real`) | Before size | After size | Optimization responsible |
|---|---:|---:|---:|---:|---|
| `recsys-dataflow-cli` | Fill from `.docker-metrics/before/dataflow-cli-build.log` | Fill from `.docker-metrics/after/dataflow-cli-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + selective runtime copy + `uv` concurrency |
| `recsys-data-generator` | Fill from `.docker-metrics/before/data-generator-build.log` | Fill from `.docker-metrics/after/data-generator-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + selective runtime copy + `uv` concurrency |
| `recsys-airflow` | Fill from `.docker-metrics/before/airflow-build.log` | Fill from `.docker-metrics/after/airflow-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage provider install + selective DAG/runtime copy |
| `recsys-spark` | Fill from `.docker-metrics/before/spark-build.log` | Fill from `.docker-metrics/after/spark-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage parallel JAR download + selective runtime copy |
| `recsys-flink` | Fill from `.docker-metrics/before/flink-build.log` | Fill from `.docker-metrics/after/flink-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage parallel JAR download + selective runtime copy |
| `recsys-api-serving` | Fill from `.docker-metrics/before/api-serving-build.log` | Fill from `.docker-metrics/after/api-serving-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + API-only runtime copy + `uv` concurrency |
| `recsys-mlops-training` | Fill from `.docker-metrics/before/mlops-training-build.log` | Fill from `.docker-metrics/after/mlops-training-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + ML/runtime-only copy + `uv` concurrency |
