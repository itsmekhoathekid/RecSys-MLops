# Improve the data generator

## Simulate Data drift

Code reference:

- [configs/local/data_generator_drift.yaml line 60](../../../configs/local/data_generator_drift.yaml#60): drift configuration block that enables the generator drift scenario.
- [configs/local/data_generator_drift.yaml line 63](../../../configs/local/data_generator_drift.yaml#63): `drift_start_date` boundary used to separate baseline/pre-drift data from post-drift data.
- [apps/data-platform/data-generator/src/drift/controller.py line 8](../../../apps/data-platform/data-generator/src/drift/controller.py#8): `DriftController` computes the drift phase and drift factor for each generated date.
- [apps/data-platform/data-generator/src/drift/reporting.py line 137](../../../apps/data-platform/data-generator/src/drift/reporting.py#137): writes drift artifacts, including feature reports, health metrics, and alert rows.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 135](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#135): prints the `Generator Configuration` table for screenshot proof.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 159](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#159): prints the `Drift Health Sample` table to show drift status, PSI, and drift factor after generation.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_drift.yaml

UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py \
  --config configs/local/data_generator_drift.yaml \
  | awk '
      /^## Generator Configuration/ {show=1}
      /^## Label Table/ {show=0}
      show {print}
    '
```

Description of output when running command:

- The generator command creates run `drift_50k_seed42` with `1,000` users, `500` products, `150` history days, and `50,000` target behavior events.
- The filtered summary output shows only the drift-related proof tables.
- `Generator Configuration` has `14` configuration rows, including run id, seed, history window, drift scenario, drift start date, drift mode, multiplier, ramp-up days, and PSI threshold.
- `Drift Health Sample` has `5` rows for key dates: baseline start, baseline end, drift start, ramp-up end, and history end.
- The drift health table illustrates `feature_name`, `mean`, `psi_vs_baseline`, `drift_status`, and `drift_factor` for `f_user_purchase_count_90d`.

Image proof:

![Data & ML system](../../pngs/data_gen_config_drift.png)

## Table with 2 columns : id and label

Code reference:

- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 46](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#46): builds label rows from the generated `users` and `orders` tables.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 51](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#51): counts each user's orders after the drift start date.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 56](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#56): creates the label table with the two main columns: `user_id` and `label`.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 84](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#84): loads the label table for proof output and merge logic.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 95](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#95): joins labels with feature rows by `user_id`.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 175](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#175): prints the `Label Table` proof with only `user_id,label`.
- [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 183](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#183): prints the `Merged Features With Labels` table to prove labels were joined with the feature table.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py \
  --config configs/local/data_generator_drift.yaml \
  | awk '
      /^## Label Table/ {show=1}
      show {print}
    '
```

Description of output when running command:

- Run this command after the drift dataset has been generated by the previous section.
- The filtered summary output shows only the label-related proof tables.
- The script builds the full label set for all `1,000` users, then prints a `Label Table` sample with `10` rows: `5` positive labels and `5` negative labels.
- The label proof also prints the label definition and distribution, for example `positive=684` and `negative=316` in the current generated run.
- `Merged Features With Labels` has `12` sample rows from the latest feature date, showing `user_id`, `label`, `feature_date`, user feature columns, and `feature_version`.
- The merged table illustrates that the two-column label table can be joined back to the feature table by `user_id` for training data preparation.

Image proof:

![Data & ML system](../../pngs/table_2_columns.png)