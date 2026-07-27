#!/usr/bin/env bash

build_api() {
  build_image "recsys-api-serving" "apps/api-serving/Dockerfile"
}
