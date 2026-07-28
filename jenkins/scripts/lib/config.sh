#!/usr/bin/env bash

gcp_production_field() {
  python3 jenkins/python/configuration.py gcp "$1"
}
