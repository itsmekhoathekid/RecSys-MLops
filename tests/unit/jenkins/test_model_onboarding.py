import json
import io
import struct
from copy import deepcopy

import httpx
import pytest

from jenkins.python.llm_agent_cd import llm_ab_start
from jenkins.python.llm_agent_cd import onboarding as onboarding_module
from jenkins.python.llm_agent_cd.model_onboarding import (
    attest_huggingface,
    build_catalog,
    build_profile,
    load_policy,
    onboarding_id,
    parse_artifact_url,
    parse_gguf,
    resource_class,
)
from jenkins.python.llm_agent_cd.onboarding_compatibility import (
    CASES,
    ONBOARDING_CONTRACT,
    check,
    fixtures,
    response_format,
)
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.serving_profiles import backend_profile, validate_profile
from apps.agentic.llm_ab_router import trigger as trigger_module

pytest_plugins = ("tests.unit.jenkins.test_llm_agent_cd",)


URL = (
    "https://huggingface.co/example/model/resolve/"
    + "a" * 40
    + "/example-Q4_K_M.gguf"
)


def _string(value):
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def _gguf(metadata):
    data = b"GGUF" + struct.pack("<IQQ", 3, 0, len(metadata))
    for key, value in metadata.items():
        data += _string(key)
        if isinstance(value, str):
            data += struct.pack("<I", 8) + _string(value)
        else:
            data += struct.pack("<IQ", 10, value)
    return data


def test_pinned_huggingface_url_is_strict():
    parsed = parse_artifact_url(URL)
    assert parsed["repository"] == "example/model"
    assert parsed["revision"] == "a" * 40
    assert parsed["filename"] == "example-Q4_K_M.gguf"
    for invalid in (
        URL.replace("a" * 40, "main"),
        URL + "?download=true",
        URL.replace("https://huggingface.co", "https://example.com"),
        URL.replace(".gguf", ".safetensors"),
    ):
        with pytest.raises(ValueError):
            parse_artifact_url(invalid)


def test_huggingface_attestation_and_license_gate():
    identity = parse_artifact_url(URL)
    payload = {
        "sha": "a" * 40,
        "private": False,
        "cardData": {"license": "apache-2.0"},
        "siblings": [{"rfilename": "example-Q4_K_M.gguf",
                      "lfs": {"sha256": "b" * 64, "size": 500_000_000}}],
    }
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    ))
    result = attest_huggingface(client, identity, load_policy())
    assert result["sha256"] == "b" * 64 and "status" not in result
    payload["cardData"]["license"] = "other"
    result = attest_huggingface(client, identity, load_policy())
    assert result["status"] == "NEEDS_LICENSE_REVIEW"


def test_gguf_metadata_and_generic_profile_are_content_addressed():
    metadata = parse_gguf(_gguf({
        "general.architecture": "qwen2",
        "qwen2.context_length": 32768,
        "tokenizer.chat_template": "{% if tools %}native tool template{% endif %}",
    }), "example-Q4_K_M.gguf")
    assert metadata["architecture"] == "qwen2"
    assert metadata["context_capability"] == 32768
    assert metadata["quantization"] == "Q4_K_M"
    artifact = {"artifact_url": URL, "sha256": "b" * 64,
                "size_bytes": 500_000_000}
    profile = build_profile(artifact, metadata, load_policy())
    catalog = build_catalog(artifact, metadata, profile)
    validate_profile(catalog)
    resources, flags = backend_profile(catalog)
    assert resources["requests"] == {"cpu": "1", "memory": "2Gi"}
    assert flags == ["--jinja", "--reasoning", "off"]
    assert "reasoningBudget" not in profile and profile["reasoningMode"] == "off"
    assert digest({key: value for key, value in profile.items() if key != "profileId"}) == profile["profileId"]


def test_legacy_generic_profile_stays_valid_without_identity_rewrite():
    catalog = _catalog()
    profile = catalog["serving"]
    profile.pop("reasoningMode")
    profile["policyChecksum"] = (
        "fe1fd4822f68facb2fdb7b6ea1d7ad570fb8e47ebd96a2a409e1c4b957788451"
    )
    profile["profileId"] = digest(
        {key: value for key, value in profile.items() if key != "profileId"}
    )

    validate_profile(catalog)
    assert backend_profile(catalog)[1] == ["--jinja"]


def _catalog():
    metadata = parse_gguf(_gguf({
        "general.architecture": "qwen2",
        "qwen2.context_length": 32768,
        "tokenizer.chat_template": "{% if tools %}native tool template{% endif %}",
    }), "example-Q4_K_M.gguf")
    artifact = {"artifact_url": URL, "sha256": "b" * 64,
                "size_bytes": 500_000_000}
    return build_catalog(artifact, metadata, build_profile(artifact, metadata, load_policy()))


def test_generic_candidate_manifest_uses_embedded_template_and_no_reasoning_budget(champion):
    from apps.agentic.llm_ab_router.trigger import candidate_from_config
    from jenkins.python.llm_agent_cd.manifests import backend_resources

    catalog = _catalog()
    config = {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": champion["release_id"],
        "generation": deepcopy(champion["config"]),
        "llm_release_ref": digest(catalog),
        "experiment_type": "llm_only",
        "policy_ref": "recommendation-live-test",
    }
    candidate = candidate_from_config(champion, config, catalog, allow_live_test=True)
    deployment = backend_resources(
        candidate, "kagent", "registry/router@sha256:" + "c" * 64
    )[0]
    pod = deployment["spec"]["template"]["spec"]
    args = pod["containers"][0]["args"]
    assert "--jinja" in args
    assert "--chat-template-file" not in args
    assert args[args.index("--reasoning") + 1] == "off"
    assert "--reasoning-budget" not in args
    assert pod["containers"][0]["resources"]["requests"] == {"cpu": "1", "memory": "2Gi"}
    assert pod["initContainers"][0]["image"] == load_policy()["runtime"]["downloader_image"]


def test_size_classes_fail_closed_above_two_gibibytes():
    policy = load_policy()
    assert resource_class(1024**3, policy)["requests"]["cpu"] == "1"
    assert resource_class(1024**3 + 1, policy)["requests"]["cpu"] == "2"
    with pytest.raises(ValueError, match="HOLD_CAPACITY"):
        resource_class(2 * 1024**3 + 1, policy)


def test_onboarding_id_binds_alias_artifact_and_policy():
    artifact = {"repository": "example/model", "revision": "a" * 40,
                "filename": "x-Q4_0.gguf", "sha256": "b" * 64,
                "size_bytes": 10}
    policy = load_policy()
    value = onboarding_id("recommendation", "new-model-q4-v1", artifact, policy)
    assert value.startswith("onb-") and len(value) == 36
    assert onboarding_id("recommendation", "new-model-q4-v1", artifact, policy) == value
    with pytest.raises(ValueError):
        onboarding_id("recommendation", "UPPER", artifact, policy)


def test_onboard_cli_and_legacy_start_cli_are_unambiguous(tmp_path):
    args = llm_ab_start.parse_args([
        "onboard", "--scope", "recommendation", "--model-alias", "new-model-q4-v1",
        "--artifact-url", URL, "--output-dir", str(tmp_path),
    ])
    assert args.command == "onboard"
    old = llm_ab_start.parse_args([
        "--scope", "recommendation", "--model-alias", "qwen35-0.8b-q4",
    ])
    assert old.command == "start"


def test_jenkins_artifact_url_uses_the_active_port_forward():
    assert llm_ab_start.local_artifact_url(
        "http://127.0.0.1:4567",
        "http://recsys-jenkins.ci.svc.cluster.local:8080/job/Onboard/6/",
    ) == "http://127.0.0.1:4567/job/Onboard/6/artifact/.llm-onboarding/result.json"


def test_six_compatibility_checks_cover_native_tool_and_json_contract():
    cases = fixtures(ONBOARDING_CONTRACT)
    assert tuple(row[0] for row in cases) == CASES
    tool_message = {"tool_calls": [{"function": {
        "name": "get_personalized_recommendations",
        "arguments": '{"user_id":1001,"candidate_item_ids":null,"top_k":3}',
    }}]}
    assert check(tool_message, ("tool", {
        "user_id": 1001, "candidate_item_ids": None, "top_k": 3,
    }))
    assert not check({"tool_calls": [*tool_message["tool_calls"], *tool_message["tool_calls"]]},
                     ("tool", {"user_id": 1001, "candidate_item_ids": None, "top_k": 3}))
    assert check({"content": '{"missing":"user_id"}'}, ("missing", None))
    assert check({"content": '{"items":[]}'}, ("json", {"items": []}))
    assert response_format(("tool", {})) is None
    assert response_format(("json", {"items": []}))["type"] == "json_schema"
    assert response_format(("missing", None))["json_schema"]["strict"] is True


def test_completed_compatibility_waits_for_first_metrics_scrape(monkeypatch):
    class Driver:
        samples = 0

        def kube(self, *args):
            if args[:2] == ("get", "job"):
                return json.dumps({"status": {"succeeded": 1}})
            if args[:2] == ("top", "pod"):
                self.samples += 1
                if self.samples == 1:
                    raise RuntimeError("metrics are not ready")
                return "backend-pod 1m 256Mi\n"
            raise AssertionError(args)

    times = iter((0, 0, 0, 0, 1))
    monkeypatch.setattr(onboarding_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(onboarding_module.time, "sleep", lambda _seconds: None)

    peak = onboarding_module._wait_compatibility(
        Driver(), "compatibility-job", "backend", timeout=10
    )

    assert peak == onboarding_module.quantity("256Mi")


def test_poller_requires_ready_catalog_attestation(monkeypatch, champion):
    catalog, ref = _catalog(), digest(_catalog())
    config = {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": champion["release_id"],
        "generation": deepcopy(champion["config"]),
        "llm_release_ref": ref,
        "experiment_type": "llm_only",
        "policy_ref": "recommendation-live-test",
    }
    monkeypatch.setenv("AB_ALLOW_LIVE_TEST", "true")

    class S3:
        objects = {"recommendation/catalog/" + ref + ".json": catalog}

        def get_object(self, **kwargs):
            if kwargs["Key"] not in self.objects:
                raise KeyError(kwargs["Key"])
            return {"Body": io.BytesIO(json.dumps(self.objects[kwargs["Key"]]).encode())}

    storage = S3()
    monkeypatch.setattr(trigger_module, "s3_client", lambda: storage)
    service = trigger_module.Trigger.__new__(trigger_module.Trigger)
    with pytest.raises(ValueError, match="lacks model-onboarding"):
        service._catalog_and_candidate({"champion": champion}, config)
    storage.objects["recommendation/catalog-attestations/" + ref + ".json"] = {
        "scope": "recommendation", "llm_release_ref": ref, "status": "READY",
    }
    llm, candidate = service._catalog_and_candidate({"champion": champion}, config)
    assert llm == catalog and candidate["llm_version_id"] == ref


def test_jenkins_onboarding_is_locked_and_does_not_route():
    source = open("jenkins/LLMCandidateOnboard.Jenkinsfile").read()
    assert "lock(resource: 'recsys-production-release')" in source
    assert "llm_agent_cd.onboarding" in source
    assert "route(" not in source and "20-case" not in source


def test_onboarding_intent_resume_ignores_only_created_at():
    from botocore.exceptions import ClientError
    from jenkins.python.llm_agent_cd.llm_ab_start import persist_intent

    original = {"onboarding_id": "onb-1", "candidate": {"release_id": "r1"},
                "created_at": 1.0}

    class S3:
        def put_object(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"},
                 "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(original).encode())}

    assert persist_intent(S3(), "bucket", "key", {**original, "created_at": 2.0}) == original
    with pytest.raises(ValueError, match="intent collision"):
        persist_intent(
            S3(), "bucket", "key",
            {"onboarding_id": "onb-1", "candidate": {"release_id": "r2"},
             "created_at": 2.0},
        )
