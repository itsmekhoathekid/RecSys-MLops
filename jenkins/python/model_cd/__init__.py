"""Atomic KServe model continuous-delivery package."""

from jenkins.python.model_cd.config import write_values
from jenkins.python.model_cd.helm_release import crd_exists, deploy, run
from jenkins.python.model_cd.manifests import (
    REQUIRED_MODEL_FILES,
    copy_s3_prefix,
    latest_storage_uri,
    parse_s3_uri,
    read_manifest,
    s3_client,
    upload_manifest,
    verify_model_repository,
)
from jenkins.python.model_cd.promotion_gates import (
    GateDecision,
    assert_promote_gates,
    evaluate_candidate_gates,
    query_prometheus,
)


def main() -> int:
    """Run the CLI without importing it during ``python -m`` package startup."""
    from jenkins.python.model_cd.cli import main as cli_main

    return cli_main()


__all__ = [
    "GateDecision",
    "REQUIRED_MODEL_FILES",
    "assert_promote_gates",
    "copy_s3_prefix",
    "crd_exists",
    "deploy",
    "evaluate_candidate_gates",
    "latest_storage_uri",
    "main",
    "parse_s3_uri",
    "query_prometheus",
    "read_manifest",
    "run",
    "s3_client",
    "upload_manifest",
    "verify_model_repository",
    "write_values",
]
