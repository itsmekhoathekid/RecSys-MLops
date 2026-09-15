from copy import deepcopy
import io
import json

import httpx
import pytest

from jenkins.python.llm_agent_cd import llm_ab_start as module
from jenkins.python.llm_agent_cd.release import release
pytest_plugins = ("tests.unit.jenkins.test_llm_agent_cd",)


class S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        if kwargs["Key"] in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412},
                               "Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": "etag"}

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def hf_response(sha=module.ARTIFACT_SHA256, size=module.ARTIFACT_SIZE):
    return httpx.Response(200, json={"sha": module.REVISION, "siblings": [{
        "rfilename": module.FILENAME, "lfs": {"sha256": sha, "size": size}
    }]})


def test_allowlisted_profile_has_expected_release_ref():
    llm = module.profile_manifest(module.REQUESTED_MODEL, module.REVISION,
                                  module.FILENAME, module.PROFILE)
    from jenkins.python.llm_agent_cd.release import digest
    assert digest(llm) == module.EXPECTED_REF
    with pytest.raises(ValueError, match="allowlisted"):
        module.profile_manifest("untrusted/model", module.REVISION, module.FILENAME, module.PROFILE)


def test_short_alias_resolves_only_to_reviewed_immutable_tuple():
    assert module.resolve_model_alias("qwen35-0.8b-q4") == {
        "model": module.REQUESTED_MODEL,
        "revision": module.REVISION,
        "filename": module.FILENAME,
        "profile": module.PROFILE,
    }
    with pytest.raises(ValueError, match="not allowlisted"):
        module.resolve_model_alias("qwen35-main-latest")


def test_previous_production_q4_alias_is_reviewed_and_immutable():
    selected = module.resolve_model_alias("qwen25-0.5b-q4-terminal-v4")
    assert selected == {
        "model": module.QWEN25_REQUESTED_MODEL,
        "revision": module.QWEN25_REVISION,
        "filename": module.QWEN25_FILENAME,
        "profile": module.QWEN25_PROFILE,
    }
    llm = module.profile_manifest(**{
        "model": selected["model"],
        "revision": selected["revision"],
        "filename": selected["filename"],
        "profile": selected["profile"],
    })
    assert module.digest(llm) == module.QWEN25_EXPECTED_REF


def test_cli_alias_and_verbose_modes_are_unambiguous():
    args = module.parse_args([
        "--scope", "recommendation", "--model-alias", "qwen35-0.8b-q4",
    ])
    assert args.model_alias == "qwen35-0.8b-q4" and args.model is None
    with pytest.raises(SystemExit):
        module.parse_args([
            "--scope", "recommendation", "--model-alias", "qwen35-0.8b-q4",
            "--profile", module.PROFILE,
        ])
    with pytest.raises(SystemExit):
        module.parse_args([
            "--scope", "recommendation", "--model", module.REQUESTED_MODEL,
        ])


def test_stock_profile_is_separately_allowlisted_and_immutable():
    from jenkins.python.llm_agent_cd.release import digest

    llm = module.profile_manifest(
        module.REQUESTED_MODEL,
        module.REVISION,
        module.FILENAME,
        module.STOCK_PROFILE,
    )
    assert digest(llm) == module.STOCK_EXPECTED_REF
    assert module.STOCK_EXPECTED_REF != module.EXPECTED_REF


def test_huggingface_attestation_fails_closed():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: hf_response("0" * 64)))
    with pytest.raises(ValueError, match="checksum or size"):
        module.verify_huggingface(client, module.profile_manifest(
            module.REQUESTED_MODEL, module.REVISION, module.FILENAME, module.PROFILE))


def test_register_is_create_only_readable_and_keeps_generation(monkeypatch, champion):
    current = release(deepcopy(champion))
    state = {"phase": "COMPLETED", "champion": current}

    monkeypatch.setattr(module, "read_state", lambda *_: deepcopy(state))
    cd, poller = S3(), S3()
    # The poller has the same object view but a distinct credential/client.
    poller.objects = cd.objects
    hf = httpx.Client(transport=httpx.MockTransport(lambda request: hf_response()))
    result = module.register(
        "recommendation", module.REQUESTED_MODEL, module.REVISION, module.FILENAME,
        module.PROFILE, cd_client=cd, poller_client=poller,
        state_uri="s3://recsys-llm-ab/recommendation/state.json", hf_client=hf,
    )
    assert result["status"] == "REGISTERED"
    assert result["llm_release_ref"] == module.EXPECTED_REF
    assert result["langfuse_config"]["generation"] == current["config"]
    assert result["langfuse_config"]["experiment_type"] == "llm_only"
    # Idempotent rerun reads the exact same immutable bytes.
    again = module.register(
        "recommendation", module.REQUESTED_MODEL, module.REVISION, module.FILENAME,
        module.PROFILE, cd_client=cd, poller_client=poller,
        state_uri="s3://recsys-llm-ab/recommendation/state.json", hf_client=hf,
    )
    assert again == result
    stored = json.loads(next(iter(cd.objects.values())))
    assert stored["artifact_sha256"] == module.ARTIFACT_SHA256


def test_alias_can_retry_a_disabled_recommendation_release(monkeypatch, champion):
    current = release(deepcopy(champion))
    llm = module.profile_manifest(
        module.REQUESTED_MODEL, module.REVISION, module.FILENAME, module.PROFILE
    )
    config = {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": current["release_id"],
        "generation": deepcopy(current["config"]),
        "llm_release_ref": module.EXPECTED_REF,
        "experiment_type": "llm_only",
        "policy_ref": "recommendation-live-test",
    }
    from apps.agentic.llm_ab_router.trigger import candidate_from_config
    candidate = candidate_from_config(current, config, llm, allow_live_test=True)
    state = {
        "phase": "ROLLED_BACK",
        "champion": current,
        "disabled": [candidate["release_id"]],
    }
    monkeypatch.setattr(module, "read_state", lambda *_: deepcopy(state))
    cd, poller = S3(), S3()
    poller.objects = cd.objects
    hf = httpx.Client(transport=httpx.MockTransport(lambda request: hf_response()))
    result = module.register_alias(
        "recommendation", "qwen35-0.8b-q4", cd_client=cd,
        poller_client=poller,
        state_uri="s3://recsys-llm-ab/recommendation/state.json", hf_client=hf,
    )
    assert result["status"] == "REGISTERED"
    assert result["model_alias"] == "qwen35-0.8b-q4"
    assert result["candidate_release_id"] == candidate["release_id"]
    assert result["langfuse_config"] == config
    assert json.loads(next(iter(cd.objects.values()))) == llm
