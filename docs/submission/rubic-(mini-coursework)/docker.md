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

| Image | Optimization used | Why it reduces image/runtime cost |
|---|---|---|
| `recsys-base-python` | `python:3.11-slim`, `--no-install-recommends`, removes `/var/lib/apt/lists` | Avoids full Debian/Python image and removes apt metadata. |
| `recsys-dataflow-cli` | Reuses shared base image and installs with `uv pip install --no-cache` | Avoids repeated base OS layers and pip cache layers. |
| `recsys-spark` | Uses official Spark runtime and adds only required Iceberg/Hudi/S3 jars | Keeps Spark runtime compatible while avoiding unrelated system packages. |
| `recsys-flink` | Installs only required PyFlink, Kafka, Iceberg, Hadoop S3, Redis dependencies | Keeps the streaming image focused on the Flink job path. |
| `recsys-api-serving` | Copies only `apps/api-serving` instead of the full repository | Smaller serving image and narrower attack surface. |

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

