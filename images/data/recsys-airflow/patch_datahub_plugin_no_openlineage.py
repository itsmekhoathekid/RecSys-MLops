"""Make DataHub plugin 1.6.0 lazy-load optional OpenLineage extractors.

The upstream 1.6.0 Airflow 2 listener imports extractor compatibility modules
at module load time even when ``enable_extractors=false``. The base wheel does
not declare an OpenLineage dependency, so the listener otherwise cannot load.
This exact-version patch keeps declared DataHub inlets/outlets available without
installing or activating an OpenLineage implementation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    if source.count(old) != 1:
        raise RuntimeError(f"Unexpected DataHub plugin 1.6.0 source in {path}")
    path.write_text(source.replace(old, new, 1))


spec = importlib.util.find_spec("datahub_airflow_plugin")
if spec is None or not spec.submodule_search_locations:
    raise RuntimeError("acryl-datahub-airflow-plugin is not installed")
root = Path(next(iter(spec.submodule_search_locations))) / "airflow2"

listener = root / "datahub_listener.py"
replace_once(
    listener,
    "from datahub_airflow_plugin.airflow2._extractors import ExtractorManager\n",
    "# ExtractorManager is imported lazily when extractors are enabled.\n",
)
replace_once(
    listener,
    """        # Create extractor_manager for Airflow 2.x with patch/extractor configuration
        self.extractor_manager = ExtractorManager(
            patch_sql_parser=self.config.patch_sql_parser,
            patch_snowflake_schema=self.config.patch_snowflake_schema,
            extract_athena_operator=self.config.extract_athena_operator,
            extract_bigquery_insert_job_operator=self.config.extract_bigquery_insert_job_operator,
            extract_teradata_operator=self.config.extract_teradata_operator,
        )
""",
    """        # OpenLineage is optional when declared inlets/outlets are the only source.
        self.extractor_manager = None
        if self.config.enable_extractors:
            from datahub_airflow_plugin.airflow2._extractors import ExtractorManager

            self.extractor_manager = ExtractorManager(
                patch_sql_parser=self.config.patch_sql_parser,
                patch_snowflake_schema=self.config.patch_snowflake_schema,
                extract_athena_operator=self.config.extract_athena_operator,
                extract_bigquery_insert_job_operator=self.config.extract_bigquery_insert_job_operator,
                extract_teradata_operator=self.config.extract_teradata_operator,
            )
""",
)

shims = root / "_shims.py"
replace_once(
    shims,
    """else:
    # Import from native apache-airflow-providers-openlineage package
    from datahub_airflow_plugin.airflow2._provider_shims import (
        OpenLineagePlugin,
        TaskHolder,
        get_operator_class,
        redact_with_exclusions,
        try_import_from_string,
    )
""",
    """else:
    # The provider is optional when DataHub extractors are disabled.
    try:
        from datahub_airflow_plugin.airflow2._provider_shims import (
            OpenLineagePlugin,
            TaskHolder,
            get_operator_class,
            redact_with_exclusions,
            try_import_from_string,
        )
    except ImportError:
        OpenLineagePlugin = None
        TaskHolder = None
        redact_with_exclusions = None

        def get_operator_class(task):
            return task.__class__

        def try_import_from_string(path):
            return None
""",
)
