# Great Expectations Contracts

This directory stores the local data quality contract for the Kubernetes data
platform. The executable runner lives at
`apps/data-platform/src/validate/great_expectations_runner.py` so it can share
warehouse connection and monitoring writers with the rest of the platform.

The first suite validates warehouse `staging` tables after Flink processing and
before dbt promotes data to `production`.

