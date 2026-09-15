"""Verify the five-stage graph and failure boundaries of grouped RAG commands."""

import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest
from jinja2 import Template


DAG_PATH = Path(
    "apps/data-platform/src/orchestration/airflow/dags/recsys_rag_item_index.py"
)


@pytest.fixture
def rag_tasks(monkeypatch):
    tasks = {}

    class Task:
        def __init__(self, task_id, image, command, **kwargs):
            self.command = command
            self.downstream = []
            self.task_id = task_id
            tasks[task_id] = self

        def __rshift__(self, other):
            self.downstream.append(other.task_id)
            return other

    class Dag:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(
        sys.modules,
        "orchestration.airflow.spark_utils",
        SimpleNamespace(
            DAG=Dag,
            DATAHUB_OPS_IMAGE="datahub",
            RAG_INDEXER_IMAGE="indexer",
            datetime=lambda *args, **kwargs: None,
            datahub_validation_command=lambda *args: "publish-datahub",
            env_schedule=lambda name, default: default,
            pod_task=Task,
        ),
    )
    runpy.run_path(str(DAG_PATH))
    return tasks


def test_rag_graph_matches_documented_five_stages(rag_tasks):
    expected = [
        "semantic_chunk_items",
        "embed_item_chunks",
        "incremental_upsert_index",
        "validate_and_publish_index",
        "publish_datahub_validation",
    ]
    assert list(rag_tasks) == expected
    for index, task_id in enumerate(expected):
        assert rag_tasks[task_id].downstream == expected[index + 1 : index + 2]


def run_stage(task, tmp_path, failure="", source="auto"):
    log = tmp_path / "commands.log"
    command = Template(task.command).render(
        params={"source_run_id": source}, ts_nodash="20260906T023000"
    )
    # Match pod_task's shell mode, replacing only the external CLI process.
    stub = """
python() {
    printf '%s\\n' "$*" >> "$COMMAND_LOG"
    if [[ "$3" == "$FAIL_COMMAND" ]]; then return 7; fi
    if [[ "$3" == "resolve-source" ]]; then printf '%s\\n' 'canonical-complete'; fi
}
"""
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail; " + stub + command],
        env={**os.environ, "COMMAND_LOG": str(log), "FAIL_COMMAND": failure},
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines()
    return result, calls


@pytest.mark.parametrize("source", ["auto", "explicit-source"])
def test_chunking_consumes_resolved_source(rag_tasks, tmp_path, source):
    result, calls = run_stage(
        rag_tasks["semantic_chunk_items"], tmp_path, source=source
    )
    assert result.returncode == 0, result.stderr
    assert f"--source-run-id {source}" in calls[0]
    assert "--source-run-id canonical-complete" in calls[1]
    assert "--run-id rag-20260906T023000" in calls[1]


def test_source_failure_stops_before_chunking(rag_tasks, tmp_path):
    result, calls = run_stage(
        rag_tasks["semantic_chunk_items"], tmp_path, "resolve-source"
    )
    assert result.returncode != 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure, expected, succeeds",
    [
        ("", ["validate-index", "verify-active-index"], True),
        ("validate-index", ["validate-index"], False),
        (
            "verify-active-index",
            ["validate-index", "verify-active-index", "rollback-index"],
            False,
        ),
    ],
)
def test_publication_failure_boundaries(
    rag_tasks, tmp_path, failure, expected, succeeds
):
    result, calls = run_stage(
        rag_tasks["validate_and_publish_index"], tmp_path, failure
    )
    assert (result.returncode == 0) is succeeds
    assert [call.split()[2] for call in calls] == expected
