#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/build/runtime.sh
source jenkins/scripts/build/engine.sh
source jenkins/scripts/build/data.sh
source jenkins/scripts/build/ml.sh
source jenkins/scripts/build/serving.sh
source jenkins/scripts/build/demo.sh
source jenkins/scripts/build/analytics.sh
source jenkins/scripts/build/dispatch.sh

build_runtime_initialize "${component}"
build_dispatch "${component}"
recsys_log "wrote image manifest: ${BUILD_MANIFEST_PATH}"
