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

Capture build latency from the `real` line in each `.log` file and image size from the generated `*-image-size.txt` file.

If only image-size evidence is needed, capture it with stable `:before` and `:after` tags so the optimized build does not overwrite the baseline image.

Before building, check free disk and Docker's local storage. If Docker Desktop reports `no space left on device` or containerd/blob `input/output error`, free disk first and restart Docker Desktop before rebuilding.

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

df -h .
docker system df

# Cleanup option for local proof builds. This removes unused build cache,
# stopped containers, unused networks, dangling/unused images, and volumes.
docker builder prune -af
docker system prune -af --volumes
```

Run this before applying the Dockerfile optimization:

```bash
set -euo pipefail
cd /Users/KHOAI/anhkhoa/RecSys-MLops
mkdir -p .docker-metrics

docker build -t recsys-base-python:before -f infra/docker/Dockerfile.base-python .
docker build -t recsys-dataflow-cli:before \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/data-platform/Dockerfile.dataflow-cli .
docker build -t recsys-data-generator:before \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/data-platform/data-generator/Dockerfile .
docker build -t recsys-airflow:before -f infra/docker/Dockerfile.airflow .
docker build -t recsys-spark:before -f apps/data-platform/Dockerfile.spark .
docker build -t recsys-flink:before -f apps/data-platform/Dockerfile.flink .
docker build -t recsys-api-serving:before -f apps/api-serving/Dockerfile .
docker build -t recsys-mlops-training:before \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:before \
  -f apps/ml-system/Dockerfile.training .

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

write_image_sizes .docker-metrics/image-size-before.txt \
  recsys-base-python:before \
  recsys-dataflow-cli:before \
  recsys-data-generator:before \
  recsys-airflow:before \
  recsys-spark:before \
  recsys-flink:before \
  recsys-api-serving:before \
  recsys-mlops-training:before
```

Run this after applying the Dockerfile optimization:

```bash
set -euo pipefail
cd /Users/KHOAI/anhkhoa/RecSys-MLops
mkdir -p .docker-metrics

docker build -t recsys-base-python:after -f infra/docker/Dockerfile.base-python .
docker build -t recsys-dataflow-cli:after \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/data-platform/Dockerfile.dataflow-cli .
docker build -t recsys-data-generator:after \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/data-platform/data-generator/Dockerfile .
docker build -t recsys-airflow:after -f infra/docker/Dockerfile.airflow .
docker build -t recsys-spark:after \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.spark .
docker build -t recsys-flink:after \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.flink .
docker build -t recsys-api-serving:after -f apps/api-serving/Dockerfile .
docker build -t recsys-mlops-training:after \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:after \
  -f apps/ml-system/Dockerfile.training .

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

write_image_sizes .docker-metrics/image-size-after.txt \
  recsys-base-python:after \
  recsys-dataflow-cli:after \
  recsys-data-generator:after \
  recsys-airflow:after \
  recsys-spark:after \
  recsys-flink:after \
  recsys-api-serving:after \
  recsys-mlops-training:after
```

Compare before and after image size:

```bash
paste .docker-metrics/image-size-before.txt .docker-metrics/image-size-after.txt
```

Run this before applying the Dockerfile optimization:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

mkdir -p .docker-metrics/before

measure_build() {
  image="$1"
  logfile="$2"
  shift 2
  /usr/bin/time -p docker build --no-cache -t "$image" "$@" . \
    2>&1 | tee "$logfile"
}

measure_build recsys-base-python:local .docker-metrics/before/base-python-build.log \
  -f infra/docker/Dockerfile.base-python
measure_build recsys-dataflow-cli:local .docker-metrics/before/dataflow-cli-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/data-platform/Dockerfile.dataflow-cli
measure_build recsys-data-generator:local .docker-metrics/before/data-generator-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/data-platform/data-generator/Dockerfile
measure_build recsys-airflow:local .docker-metrics/before/airflow-build.log \
  -f infra/docker/Dockerfile.airflow
measure_build recsys-spark:local .docker-metrics/before/spark-build.log \
  -f apps/data-platform/Dockerfile.spark
measure_build recsys-flink:local .docker-metrics/before/flink-build.log \
  -f apps/data-platform/Dockerfile.flink
measure_build recsys-api-serving:local .docker-metrics/before/api-serving-build.log \
  -f apps/api-serving/Dockerfile
measure_build recsys-mlops-training:local .docker-metrics/before/mlops-training-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/ml-system/Dockerfile.training

for image in \
  recsys-base-python:local \
  recsys-dataflow-cli:local \
  recsys-data-generator:local \
  recsys-airflow:local \
  recsys-spark:local \
  recsys-flink:local \
  recsys-api-serving:local \
  recsys-mlops-training:local
do
  docker image inspect "$image" --format "$image {{.Size}}"
done | awk '{printf "%-34s %.2f MB\n", $1, $2 / 1024 / 1024}' \
  | tee .docker-metrics/before/image-size.txt
```

Run this after applying the Dockerfile optimization:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

mkdir -p .docker-metrics/after

measure_build() {
  image="$1"
  logfile="$2"
  shift 2
  /usr/bin/time -p docker build --no-cache -t "$image" "$@" . \
    2>&1 | tee "$logfile"
}

measure_build recsys-base-python:local .docker-metrics/after/base-python-build.log \
  -f infra/docker/Dockerfile.base-python
measure_build recsys-dataflow-cli:local .docker-metrics/after/dataflow-cli-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/data-platform/Dockerfile.dataflow-cli
measure_build recsys-data-generator:local .docker-metrics/after/data-generator-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/data-platform/data-generator/Dockerfile
measure_build recsys-airflow:local .docker-metrics/after/airflow-build.log \
  -f infra/docker/Dockerfile.airflow
measure_build recsys-spark:local .docker-metrics/after/spark-build.log \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.spark
measure_build recsys-flink:local .docker-metrics/after/flink-build.log \
  --build-arg DOWNLOAD_JOBS=4 \
  -f apps/data-platform/Dockerfile.flink
measure_build recsys-api-serving:local .docker-metrics/after/api-serving-build.log \
  -f apps/api-serving/Dockerfile
measure_build recsys-mlops-training:local .docker-metrics/after/mlops-training-build.log \
  --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local \
  -f apps/ml-system/Dockerfile.training

for image in \
  recsys-base-python:local \
  recsys-dataflow-cli:local \
  recsys-data-generator:local \
  recsys-airflow:local \
  recsys-spark:local \
  recsys-flink:local \
  recsys-api-serving:local \
  recsys-mlops-training:local
do
  docker image inspect "$image" --format "$image {{.Size}}"
done | awk '{printf "%-34s %.2f MB\n", $1, $2 / 1024 / 1024}' \
  | tee .docker-metrics/after/image-size.txt
```

Use this table for the screenshot/write-up:

| Image | Before build latency (`real`) | After build latency (`real`) | Before size | After size | Optimization responsible |
|---|---:|---:|---:|---:|---|
| `recsys-dataflow-cli` | Fill from `.docker-metrics/before/dataflow-cli-build.log` | Fill from `.docker-metrics/after/dataflow-cli-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + selective runtime copy + `uv` concurrency |
| `recsys-data-generator` | Fill from `.docker-metrics/before/data-generator-build.log` | Fill from `.docker-metrics/after/data-generator-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + selective runtime copy + `uv` concurrency |
| `recsys-airflow` | Fill from `.docker-metrics/before/airflow-build.log` | Fill from `.docker-metrics/after/airflow-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage provider install + selective DAG/runtime copy |
| `recsys-spark` | Fill from `.docker-metrics/before/spark-build.log` | Fill from `.docker-metrics/after/spark-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage parallel JAR download + selective runtime copy |
| `recsys-flink` | Fill from `.docker-metrics/before/flink-build.log` | Fill from `.docker-metrics/after/flink-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage parallel JAR download + selective runtime copy |
| `recsys-api-serving` | Fill from `.docker-metrics/before/api-serving-build.log` | Fill from `.docker-metrics/after/api-serving-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + API-only runtime copy + `uv` concurrency |
| `recsys-mlops-training` | Fill from `.docker-metrics/before/mlops-training-build.log` | Fill from `.docker-metrics/after/mlops-training-build.log` | Fill from `.docker-metrics/before/image-size.txt` | Fill from `.docker-metrics/after/image-size.txt` | Multi-stage venv + ML/runtime-only copy + `uv` concurrency |

## Run Commands

Local compose:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

docker compose -f infra/docker/docker-compose.dataflow.yml build
docker compose -f infra/docker/docker-compose.dataflow.yml up -d
docker compose -f infra/docker/docker-compose.dataflow.yml ps
```

GCP Cloud Build:

```bash
gcloud builds submit \
  --project fsds-coursework \
  --config infra/cloudbuild/recsys-images.yaml \
  --substitutions _IMAGE_REPO=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys,_TAG=gcp \
  --async \
  .
```

Observed GCP proof:

```text
Build ID: 5793bfd4-3733-4506-a2f1-88535ca012d0
Status: SUCCESS
Final log: DONE
Digest: sha256:569f3eb3e0bfcaa2d1068d1653e8edfeecf7aca1943fe6635c7d3b5262e082ec
```

Image proof:

![Cloud Build log](../../pngs/gcp_build_log.png)

## Image Size Evidence

Use this command to capture current local image sizes after build:

```bash
docker images | rg 'recsys-(base-python|dataflow-cli|spark|flink|api-serving)'
```

For GCP Artifact Registry proof:

```bash
gcloud artifacts docker images list \
  asia-southeast1-docker.pkg.dev/fsds-coursework/recsys \
  --include-tags \
  --filter='tags:gcp' \
  --format='table(package,tags,updateTime)'
```

Image proof to capture:

```text
docs/pngs/docker_images_size.png
docs/pngs/cicd_artifact_registry_gcp_tags.png
```
