from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from datahub.metadata.schema_classes import (
    DataJobInputOutputClass,
    DataProcessInstanceRunEventClass,
    DataProcessInstanceRunResultClass,
    DataProcessRunStatusClass,
)


def _module():
    path = Path("ops/migrations/datahub-sdk-lineage-cutover/cutover.py")
    spec = importlib.util.spec_from_file_location("datahub_sdk_lineage_cutover", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Graph:
    def __init__(self, existing=(), successful_instances=()):
        self.existing = set(existing)
        self.successful_instances = set(successful_instances)

    def exists(self, urn):
        return urn in self.existing

    def execute_graphql(self, query, variables):
        return {"entity": {"relationships": {"relationships": []}}}

    def get_aspect(self, urn, aspect):
        if urn in self.existing and aspect is DataJobInputOutputClass:
            return DataJobInputOutputClass(inputDatasets=[], outputDatasets=[])
        if (
            urn in self.successful_instances
            and aspect is DataProcessInstanceRunEventClass
        ):
            return DataProcessInstanceRunEventClass(
                timestampMillis=1,
                status=DataProcessRunStatusClass.COMPLETE,
                result=DataProcessInstanceRunResultClass(type="SUCCESS"),
            )
        return None


class _Emitter:
    def __init__(self, graph):
        self.graph = graph


def test_cutover_manifest_targets_only_legacy_jobs_and_non_airflow_flows():
    module = _module()
    legacy_entities = {
        "urn:li:dataFlow:(airflow,recsys_cdc_postgres_to_kafka,PROD)",
        "urn:li:dataFlow:(airflow,recsys_flink_stream_features,PROD)",
    }
    legacy_jobs = set()
    for product in module.products():
        old_flow = module.flow_urn(product.flow_id)
        legacy_jobs.update(
            module.job_urn(old_flow, f"{product.flow_id}.{job.id}")
            for job in product.jobs
        )
    legacy_entities.update(legacy_jobs)
    manifest = module.build_manifest(_Emitter(_Graph(existing=legacy_entities)))

    assert len(manifest["legacy_data_jobs"]) == 11
    assert len(manifest["required_new_data_jobs"]) == 11
    assert manifest["legacy_process_instances"] == []
    assert manifest["legacy_data_flows"] == [
        "urn:li:dataFlow:(airflow,recsys_cdc_postgres_to_kafka,PROD)",
        "urn:li:dataFlow:(airflow,recsys_flink_stream_features,PROD)",
    ]
    assert all(
        ",recsys_dp1_raw_to_bronze." in urn
        or ",recsys_dp2_bronze_to_silver_gold." in urn
        or ",recsys_dp3_offline_feature_table." in urn
        or ",recsys_cdc_postgres_to_kafka." in urn
        or ",recsys_flink_stream_features." in urn
        for urn in manifest["legacy_data_jobs"]
    )


def test_cutover_validation_requires_new_lineage_and_run_evidence(monkeypatch):
    module = _module()
    required = "urn:li:dataJob:(urn:li:dataFlow:(flink,flow,PROD),job)"
    manifest = {"required_new_data_jobs": [required]}
    emitter = _Emitter(_Graph(existing={required}))

    monkeypatch.setattr(module, "successful_process_instances", lambda graph, urn: [])
    with pytest.raises(RuntimeError, match="missing successful cutover run evidence"):
        module.validate_cutover(emitter, manifest)

    monkeypatch.setattr(
        module,
        "successful_process_instances",
        lambda graph, urn: ["urn:li:dataProcessInstance:test"],
    )
    module.validate_cutover(emitter, manifest)
