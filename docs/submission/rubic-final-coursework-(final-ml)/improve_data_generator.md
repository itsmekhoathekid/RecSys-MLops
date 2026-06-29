# Improve the data generator

## Simulate Data drift

Code reference:

- [configs/local/data_generator_drift.yaml line 60](../../../configs/local/data_generator_drift.yaml#60)
- [configs/local/data_generator_drift.yaml line 63](../../../configs/local/data_generator_drift.yaml#63)
- [apps/data-platform/data-generator/src/drift/controller.py line 8](../../../apps/data-platform/data-generator/src/drift/controller.py#8)
- [apps/data-platform/data-generator/src/drift/reporting.py line 137](../../../apps/data-platform/data-generator/src/drift/reporting.py#137)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 135](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#135)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 159](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#159)

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_drift.yaml

PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py \
  --config configs/local/data_generator_drift.yaml
```

Image proof:

TODO: paste screenshot showing `Generator Configuration` and `Drift Health Sample`.

## Table with 2 columns : id and label

Code reference:

- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 46](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#46)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 51](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#51)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 56](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#56)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 84](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#84)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 95](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#95)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 175](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#175)
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 183](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#183)

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_drift.yaml

PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py \
  --config configs/local/data_generator_drift.yaml
```

Image proof:

TODO: paste screenshot showing `Label Table` with `user_id,label` and `Merged Features With Labels`.
