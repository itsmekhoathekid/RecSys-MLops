"""Versioned, operator-owned serving profiles. Never normalize legacy identities."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

SMALL = "qwen25-small-cpu-v1"
SMALL_V2 = "qwen25-small-cpu-v2"
SMALL_B8646 = "qwen25-small-cpu-b8646-v3"
SMALL_B8646_TERMINAL = "qwen25-small-cpu-b8646-terminal-v4"
QWEN35_NATIVE = "qwen35-native-tools-v1"
QWEN35_NATIVE_V2 = "qwen35-native-tools-v2"
QWEN35_NATIVE_V3 = "qwen35-native-tools-v3"
QWEN35_B8646_STOCK = "qwen35-b8646-stock-adk-v1"
QWEN35_B8646_STOCK_CACHE = "qwen35-b8646-stock-adk-cache-v2"
TEMPLATE_SHA256 = "cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f"
TERMINAL_TEMPLATE_SHA256 = "242ffc1814699f2e093009747468f002127459a5a64e857254d3d95c1fdbcd3a"
QWEN35_TEMPLATE_SHA256 = "7738ebd6d0a355161b3e4a80f9d1913a12f8c39e2ff0a89775be0c86a02e85a7"
QWEN35_TEMPLATE_REVISION = "2b48083dfa97c3cbf0220cf1a5e6cffe0a511157"
QWEN35_EMBEDDED_TEMPLATE_SHA256 = "273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80"
QWEN35_ARTIFACT_REVISION = "8fea620810c4afa23dd6443f999a48574c1611a3"


def tool_template():
    data = Path(__file__).with_name("templates").joinpath("qwen25-tools-v2.jinja").read_bytes()
    if sha256(data).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("serving template checksum mismatch")
    return data.decode()


def terminal_tool_template():
    data = Path(__file__).with_name("templates").joinpath(
        "qwen25-tools-terminal-v3.jinja").read_bytes()
    if sha256(data).hexdigest() != TERMINAL_TEMPLATE_SHA256:
        raise ValueError("terminal serving template checksum mismatch")
    return data.decode()


def qwen35_tool_template():
    # Source files end with POSIX newline; the official tokenizer JSON string
    # does not. Hash and serve the exact upstream template bytes.
    data = Path(__file__).with_name("templates").joinpath(
        "qwen35-tools-official-2b48083.jinja").read_text().removesuffix("\n").encode()
    if sha256(data).hexdigest() != QWEN35_TEMPLATE_SHA256:
        raise ValueError("Qwen3.5 tool-use template checksum mismatch")
    return data.decode()


SMALL_RESOURCES = {"requests": {"cpu": "1", "memory": "1536Mi"},
                   "limits": {"cpu": "2", "memory": "2Gi"}}
QWEN35_RESOURCES = {"requests": {"cpu": "100m", "memory": "1536Mi"},
                    "limits": {"cpu": "2", "memory": "3Gi"}}


V2_FIELDS = {
    "profileId", "schemaVersion", "runtime", "imageDigest",
    "llamaCppRevision", "contextSize", "maxPredictedTokens", "parallel",
    "threads", "threadsBatch", "batchSize", "ubatchSize", "jinja",
    "reasoningMode",
    "chatTemplateSource", "chatTemplateSha256", "architecture",
    "artifactSizeBytes", "resources", "policyChecksum",
}
V2_LEGACY_FIELDS = V2_FIELDS - {"reasoningMode"}
V2_LEGACY_POLICY_CHECKSUMS = {
    "fe1fd4822f68facb2fdb7b6ea1d7ad570fb8e47ebd96a2a409e1c4b957788451",
}


def _validate_v2(llm):
    """Validate generic onboarding output without re-profiling legacy releases."""
    import re
    from .model_onboarding import load_policy
    from .release import digest

    serving = llm["serving"]
    fields = frozenset(serving)
    if fields not in {frozenset(V2_FIELDS), frozenset(V2_LEGACY_FIELDS)}:
        raise ValueError("generic serving profile v2 fields mismatch")
    material = {key: value for key, value in serving.items() if key != "profileId"}
    policy = load_policy()
    if serving["profileId"] != digest(material):
        raise ValueError("generic serving profile ID mismatch")
    current_policy = serving["policyChecksum"] == digest(policy)
    legacy_policy = (
        fields == V2_LEGACY_FIELDS
        and serving["policyChecksum"] in V2_LEGACY_POLICY_CHECKSUMS
    )
    if not current_policy and not legacy_policy:
        raise ValueError("generic serving policy checksum mismatch")
    expected = {
        "schemaVersion": 2,
        "runtime": "llama.cpp",
        "imageDigest": llm["image"],
        "llamaCppRevision": policy["runtime"]["llama_cpp_revision"],
        "contextSize": 16384,
        "maxPredictedTokens": 768,
        "parallel": 1,
        "threads": 2,
        "threadsBatch": 2,
        "batchSize": 512,
        "ubatchSize": 128,
        "jinja": True,
        "chatTemplateSource": "embedded_gguf",
    }
    if current_policy:
        expected["reasoningMode"] = policy["runtime"]["reasoning_mode"]
    if any(serving.get(key) != value for key, value in expected.items()):
        raise ValueError("generic serving profile runtime settings mismatch")
    if (not re.fullmatch(r"[0-9a-f]{64}", serving["chatTemplateSha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", serving["policyChecksum"])
            or not isinstance(serving["architecture"], str)
            or not serving["architecture"]):
        raise ValueError("generic serving profile metadata is invalid")
    pairs = [(item["requests"], item["limits"]) for item in policy["resource_classes"]]
    resources = serving["resources"]
    if set(resources) != {"requests", "limits"} or (resources["requests"], resources["limits"]) not in pairs:
        raise ValueError("generic serving resource class is not allowlisted")
    if (not isinstance(serving["artifactSizeBytes"], int)
            or serving["artifactSizeBytes"] <= 0
            or serving["artifactSizeBytes"] > policy["artifact_max_bytes"]):
        raise ValueError("generic serving artifact size is invalid")
    if not re.fullmatch(r"Q[0-9A-Z_]+", llm["quantization"]):
        raise ValueError("generic serving quantization is invalid")


def validate_profile(llm):
    if llm.get("serving", {}).get("schemaVersion") == 2:
        _validate_v2(llm)
        return
    profile = llm.get("serving", {}).get("resourceProfile")
    if profile is None:
        return  # Legacy manifests and their hashes remain untouched.
    if profile not in {SMALL, SMALL_V2, SMALL_B8646, SMALL_B8646_TERMINAL,
                       QWEN35_NATIVE, QWEN35_NATIVE_V2,
                       QWEN35_NATIVE_V3, QWEN35_B8646_STOCK,
                       QWEN35_B8646_STOCK_CACHE}:
        raise ValueError("unknown serving resource profile")
    expected = {"resourceProfile": profile, "contextSize": 16384,
                "maxPredictedTokens": 768, "parallel": 1, "threads": 2,
                "threadsBatch": 2, "batchSize": 512, "ubatchSize": 128}
    if profile in {SMALL_V2, SMALL_B8646}:
        expected["chatTemplateSha256"] = TEMPLATE_SHA256
    if profile == SMALL_B8646_TERMINAL:
        expected["chatTemplateSha256"] = TERMINAL_TEMPLATE_SHA256
    if profile in {SMALL_B8646, SMALL_B8646_TERMINAL}:
        expected.update(llamaCppBuild="b8646",
                        llamaCppRevision="0c58ba3365d2bc717b447b5d70e4d6be09ff3c40")
    if profile in {QWEN35_NATIVE, QWEN35_NATIVE_V2}:
        expected.update(chatTemplateSha256=QWEN35_TEMPLATE_SHA256,
                        chatTemplateSourceRevision=QWEN35_TEMPLATE_REVISION)
    if profile == QWEN35_NATIVE_V2:
        expected["reasoningMode"] = "off"
    if profile == QWEN35_NATIVE_V3:
        expected.update(chatTemplateSha256=QWEN35_EMBEDDED_TEMPLATE_SHA256,
                        chatTemplateSourceRevision=QWEN35_ARTIFACT_REVISION,
                        chatTemplateSource="embedded_gguf", reasoningMode="off")
    if profile == QWEN35_B8646_STOCK:
        expected.update(chatTemplateSha256=QWEN35_EMBEDDED_TEMPLATE_SHA256,
                        chatTemplateSourceRevision=QWEN35_ARTIFACT_REVISION,
                        chatTemplateSource="embedded_gguf", reasoningMode="off",
                        llamaCppBuild="b8646",
                        llamaCppRevision="0c58ba3365d2bc717b447b5d70e4d6be09ff3c40")
    if profile == QWEN35_B8646_STOCK_CACHE:
        expected.update(chatTemplateSha256=QWEN35_EMBEDDED_TEMPLATE_SHA256,
                        chatTemplateSourceRevision=QWEN35_ARTIFACT_REVISION,
                        chatTemplateSource="embedded_gguf", reasoningMode="off",
                        cacheRamMiB=256, llamaCppBuild="b8646",
                        llamaCppRevision="0c58ba3365d2bc717b447b5d70e4d6be09ff3c40")
    if llm["serving"] != expected:
        raise ValueError("small serving profile settings mismatch")
    if profile in {SMALL, SMALL_V2, SMALL_B8646, SMALL_B8646_TERMINAL}:
        if (llm["artifact_sha256"] != "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
                or llm["quantization"] != "Q4_K_M"):
            raise ValueError("small serving profile requires attested Qwen2.5 artifact")
    elif (llm["artifact_sha256"] != "57d1997790d1744fba5b40a7317df71ea5e2acee28c47e78f0cce39c0703f8cf"
          or llm["quantization"] != "Q4_0"):
        raise ValueError("native tool profile requires attested Qwen3.5 artifact")


def backend_profile(llm):
    validate_profile(llm)
    if llm["serving"].get("schemaVersion") == 2:
        # The embedded GGUF template is the only accepted source. Runtime
        # /props attestation confirms native tools before compatibility runs.
        extra = ["--jinja"]
        if llm["serving"].get("reasoningMode") == "off":
            extra.extend(["--reasoning", "off"])
        return deepcopy(llm["serving"]["resources"]), extra
    if llm["serving"].get("resourceProfile") in {
            SMALL_V2, SMALL_B8646, SMALL_B8646_TERMINAL}:
        (terminal_tool_template() if llm["serving"].get("resourceProfile")
         == SMALL_B8646_TERMINAL else tool_template())
        return deepcopy(SMALL_RESOURCES), ["--jinja", "--chat-template-file", "/chat-template/template.jinja"]
    if llm["serving"].get("resourceProfile") == SMALL:
        return deepcopy(SMALL_RESOURCES), ["--jinja"]
    if llm["serving"].get("resourceProfile") in {QWEN35_NATIVE, QWEN35_NATIVE_V2}:
        qwen35_tool_template()
        extra = ["--jinja", "--chat-template-file", "/chat-template/template.jinja"]
        if llm["serving"].get("resourceProfile") == QWEN35_NATIVE_V2:
            extra.extend(["--reasoning", "off"])
        return deepcopy(QWEN35_RESOURCES), extra
    if llm["serving"].get("resourceProfile") == QWEN35_NATIVE_V3:
        # Use the tool-capable template embedded in the checksum-pinned GGUF.
        # /props attestation verifies it before any serving probe or cutover.
        return deepcopy(QWEN35_RESOURCES), ["--jinja", "--reasoning", "off"]
    if llm["serving"].get("resourceProfile") in {
            QWEN35_B8646_STOCK, QWEN35_B8646_STOCK_CACHE}:
        # Issue #26530 reports the pre-AC-parser b8646 path as the control
        # which falls back to the JSON tool grammar for complex Qwen3.5 tool
        # prompts. Keep the embedded, attested tool template and disable
        # reasoning, exactly as in the upstream reproduction command.
        extra = ["--jinja", "--reasoning", "off"]
        if llm["serving"].get("resourceProfile") == QWEN35_B8646_STOCK_CACHE:
            extra.extend(["--cache-ram", "256"])
        return deepcopy(QWEN35_RESOURCES), extra
    return {"requests": {"cpu": "2", "memory": "3Gi"},
            "limits": {"cpu": "2", "memory": "5Gi"}}, []
