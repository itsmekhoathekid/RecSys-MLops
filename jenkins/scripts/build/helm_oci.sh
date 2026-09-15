#!/usr/bin/env bash

package_publish_helm_oci() {
  local artifact_id="$1"
  local chart_path="$2"
  local repository_path="$3"
  local image_registry="$4"
  local commit="$5"
  local publish="$6"
  local release_version package_dir package_source package_path parent_repository tagged_reference
  local digest_reference

  release_version="$(
    python3 -c 'from jenkins.python.release_plan import release_version; import sys; print(release_version(sys.argv[1]))' \
      "${commit}"
  )"
  [[ -f "${chart_path}/Chart.yaml" ]] || {
    recsys_error "Helm OCI artifact ${artifact_id} has no Chart.yaml"
    return 2
  }
  [[ "${repository_path}" == helm/* ]] || {
    recsys_error "Helm OCI artifact ${artifact_id} must use the helm/ repository prefix"
    return 2
  }

  package_dir=".ci-artifact-packages/${artifact_id}"
  package_source="${package_dir}/source"
  rm -rf -- "${package_dir}"
  mkdir -p "${package_dir}" .ci-artifact-manifest
  mkdir -p "${package_source}"
  cp -R "${chart_path}/." "${package_source}/"
  helm dependency build "${package_source}" >/dev/null
  helm package "${package_source}" \
    --version "${release_version}" \
    --app-version "${commit}" \
    --destination "${package_dir}" >/dev/null
  package_path="$(find "${package_dir}" -maxdepth 1 -type f -name "*-${release_version}.tgz" -print -quit)"
  [[ -n "${package_path}" ]] || {
    recsys_error "Helm package was not created for ${artifact_id}"
    return 2
  }
  python3 -m jenkins.python.helm_oci "${package_path}"
  helm show chart "${package_path}" >/dev/null

  if ! recsys_is_true "${publish}"; then
    recsys_log BUILD "packaged ${artifact_id}; OCI publication is disabled"
    return 0
  fi

  registry_login_gcp_helm "${image_registry}" >/dev/null
  parent_repository="${repository_path%/*}"
  tagged_reference="${image_registry}/${repository_path}:${release_version}"
  if digest_reference="$(
    registry_resolve_digest_reference "${tagged_reference}" "${image_registry}" 2>/dev/null
  )"; then
    local existing_dir existing_package
    existing_dir="${package_dir}/existing"
    mkdir -p "${existing_dir}"
    helm pull "oci://${image_registry}/${repository_path}" \
      --version "${release_version}" --destination "${existing_dir}" >/dev/null
    existing_package="$(find "${existing_dir}" -maxdepth 1 -type f -name "*-${release_version}.tgz" -print -quit)"
    [[ -n "${existing_package}" ]] || {
      recsys_error "existing Helm OCI artifact cannot be read for ${artifact_id}"
      return 2
    }
    python3 -m jenkins.python.helm_oci "${existing_package}"
    cmp -s "${package_path}" "${existing_package}" || {
      recsys_error "immutable Helm OCI tag already contains different content: ${tagged_reference}"
      return 2
    }
    recsys_log BUILD "reusing matching Helm OCI artifact ${digest_reference}"
  else
    helm push "${package_path}" "oci://${image_registry}/${parent_repository}"
    digest_reference="$(registry_resolve_digest_reference "${tagged_reference}" "${image_registry}")"
  fi
  python3 -m jenkins.python.agent_registry_release write-chart-record \
    --artifact "${artifact_id}" \
    --commit "${commit}" \
    --reference "oci://${digest_reference}" \
    --output ".ci-artifact-manifest/${artifact_id}.json"
  recsys_log BUILD "published ${artifact_id} as ${digest_reference}"
}
