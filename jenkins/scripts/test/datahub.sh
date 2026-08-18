#!/usr/bin/env bash

test_datahub_catalog() {
  local image
  image="$(resolve_release_image recsys-data-ingestion)"
  datahub_catalog_verify "${image}"
}
