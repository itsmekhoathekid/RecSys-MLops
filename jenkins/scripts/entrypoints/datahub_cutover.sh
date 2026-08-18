#!/usr/bin/env bash
set -euo pipefail

mode="${1:?cutover mode is required}"
manifest="${2:-.ci-deploy/datahub-dataset-lineage-cutover.json}"

source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/datahub.sh

image="$(resolve_release_image recsys-data-ingestion)"
datahub_catalog_cutover "${mode}" "${manifest}" "${image}"
if [[ "${mode}" == "plan" ]]; then
  python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); print(", ".join(f"{key}={value}" for key, value in sorted(data.get("counts", {}).items())))' \
    "${manifest}" > "${manifest}.counts"
fi
