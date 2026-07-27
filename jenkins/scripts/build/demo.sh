#!/usr/bin/env bash

build_demo() {
  build_image "recsys-demo-api" "apps/demo-web/backend/Dockerfile"
  build_image "recsys-demo-web" "apps/demo-web/frontend/Dockerfile"
}
