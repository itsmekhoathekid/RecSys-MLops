"""Pure model-onboarding rules: pinned HF identity, GGUF metadata and profile v2.

This module deliberately has no Kubernetes, Jenkins, PostgreSQL or S3 calls.
Those side effects live in :mod:`onboarding`, which keeps validation easy to
test and makes the command-line entrypoint a small adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from urllib.parse import unquote, urlparse

from .release import digest


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "configs/llm-ab/onboarding-policy.json"
ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(Q(?:[2-8]_K_[SML]|[2-8]_[01]|[2-8]))(?=[-_.]|$)", re.I
)


class IncompleteGGUF(ValueError):
    """The bounded prefix ended before all GGUF metadata was available."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_policy(path: str | Path = DEFAULT_POLICY) -> dict:
    value = json.loads(Path(path).read_text())
    runtime = value.get("runtime", {})
    if value.get("schema_version") != 1:
        raise ValueError("unsupported onboarding policy")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", runtime.get("image", "")):
        raise ValueError("onboarding runtime image must be digest pinned")
    if not COMMIT_RE.fullmatch(runtime.get("llama_cpp_revision", "")):
        raise ValueError("llama.cpp revision must be commit pinned")
    if runtime.get("reasoning_mode") != "off":
        raise ValueError("onboarding reasoning mode must be disabled")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", runtime.get("downloader_image", "")):
        raise ValueError("onboarding downloader image must be digest pinned")
    if value.get("prepared_ttl_seconds") != 7200:
        raise ValueError("prepared candidate TTL policy drift")
    return value


def parse_artifact_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "huggingface.co" or parsed.query or parsed.fragment:
        raise ValueError("artifact URL must be a query-free public huggingface.co HTTPS URL")
    match = re.fullmatch(r"/([^/]+/[^/]+)/resolve/([0-9a-f]{40})/(.+\.gguf)", unquote(parsed.path), re.I)
    if not match or ".." in match.group(3).split("/"):
        raise ValueError("artifact URL must pin a 40-hex revision and one GGUF file")
    repository, revision, filename = match.groups()
    return {
        "artifact_url": url,
        "repository": repository,
        "revision": revision.lower(),
        "filename": filename,
    }


def _license(payload: dict) -> str:
    value = (payload.get("cardData") or {}).get("license")
    if not value:
        value = next((tag.split(":", 1)[1] for tag in payload.get("tags", [])
                      if tag.startswith("license:")), "")
    return str(value).lower()


def attest_huggingface(client, identity: dict, policy: dict) -> dict:
    response = client.get(
        f"https://huggingface.co/api/models/{identity['repository']}/revision/{identity['revision']}",
        params={"blobs": "true"}, timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("private") is True or payload.get("sha") != identity["revision"]:
        raise ValueError("Hugging Face repository is private or revision attestation changed")
    matches = [item for item in payload.get("siblings", [])
               if item.get("rfilename") == identity["filename"]]
    if len(matches) != 1:
        raise ValueError("GGUF file is missing or ambiguous at the pinned revision")
    lfs = matches[0].get("lfs") or {}
    sha256 = lfs.get("sha256") or matches[0].get("sha256")
    size = lfs.get("size") or matches[0].get("size")
    if not SHA256_RE.fullmatch(str(sha256 or "")) or not isinstance(size, int) or size <= 0:
        raise ValueError("Hugging Face did not expose immutable LFS checksum and size")
    license_id = _license(payload)
    result = {**identity, "sha256": sha256, "size_bytes": size, "license": license_id}
    if license_id not in policy["allowed_licenses"]:
        result["status"] = "NEEDS_LICENSE_REVIEW"
    return result


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.offset = data, 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise IncompleteGGUF("GGUF metadata exceeds downloaded prefix")
        value, self.offset = self.data[self.offset:end], end
        return value

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def string(self) -> str:
        size = self.unpack("Q")
        if size > 16 * 1024 * 1024:
            raise ValueError("GGUF metadata string exceeds safety bound")
        return self.take(size).decode("utf-8")


_SCALARS = {
    0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
    6: "f", 7: "?", 10: "Q", 11: "q", 12: "d",
}


def _value(reader: _Reader, value_type: int, depth: int = 0):
    if depth > 2:
        raise ValueError("unsupported nested GGUF metadata array")
    if value_type in _SCALARS:
        return reader.unpack(_SCALARS[value_type])
    if value_type == 8:
        return reader.string()
    if value_type == 9:
        element_type, count = reader.unpack("I"), reader.unpack("Q")
        if count > 1_000_000:
            raise ValueError("GGUF metadata array exceeds safety bound")
        return [_value(reader, element_type, depth + 1) for _ in range(count)]
    raise ValueError(f"unsupported GGUF metadata type {value_type}")


def parse_gguf(data: bytes, filename: str) -> dict:
    reader = _Reader(data)
    if reader.take(4) != b"GGUF":
        raise ValueError("artifact is not GGUF")
    version = reader.unpack("I")
    if version not in {2, 3}:
        raise ValueError("unsupported GGUF version")
    reader.unpack("Q")  # tensor count; tensor data is intentionally not parsed.
    count = reader.unpack("Q")
    if count > 100_000:
        raise ValueError("GGUF metadata count exceeds safety bound")
    metadata = {}
    for _ in range(count):
        key = reader.string()
        metadata[key] = _value(reader, reader.unpack("I"))
    architecture = metadata.get("general.architecture")
    context_values = [v for k, v in metadata.items() if k.endswith(".context_length")]
    template = metadata.get("tokenizer.chat_template")
    if isinstance(template, str):
        try:
            parsed = json.loads(template)
            if isinstance(parsed, dict):
                template = parsed.get("tool_use") or parsed.get("default")
        except json.JSONDecodeError:
            pass
    quant = QUANT_RE.search(Path(filename).name)
    if not architecture or not context_values or not isinstance(template, str) or not template.strip():
        raise ValueError("GGUF lacks architecture, context length or embedded chat template")
    if not quant:
        raise ValueError("GGUF filename must identify its quantization")
    return {
        "gguf_version": version,
        "architecture": architecture,
        "context_capability": max(int(value) for value in context_values),
        "quantization": quant.group(1).upper(),
        "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
    }


def _allowed_redirect(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "huggingface.co" or any(host == suffix or host.endswith("." + suffix)
                                            for suffix in ("hf.co", "xethub.hf.co"))


def fetch_gguf_metadata(client, identity: dict, policy: dict) -> dict:
    size = 64 * 1024
    limit = int(policy["gguf_metadata_max_bytes"])
    while size <= limit:
        with client.stream("GET", identity["artifact_url"],
                           headers={"Range": f"bytes=0-{size - 1}"}, timeout=60) as response:
            response.raise_for_status()
            if any(not _allowed_redirect(str(item.url)) for item in [*response.history, response]):
                raise ValueError("artifact redirect left the approved Hugging Face CDN")
            chunks, received = [], 0
            for chunk in response.iter_bytes():
                chunks.append(chunk[:max(0, size - received)])
                received += len(chunks[-1])
                if received >= size:
                    break
            data = b"".join(chunks)
        try:
            return parse_gguf(data, identity["filename"])
        except IncompleteGGUF:
            size *= 2
    raise ValueError("GGUF metadata exceeds bounded inspection limit")


def resource_class(size_bytes: int, policy: dict) -> dict:
    for item in policy["resource_classes"]:
        if size_bytes <= item["max_bytes"]:
            return {"requests": item["requests"], "limits": item["limits"]}
    raise ValueError("HOLD_CAPACITY: GGUF exceeds the approved 2 GiB cluster class")


def build_profile(artifact: dict, metadata: dict, policy: dict) -> dict:
    minimum = int(policy["minimum_context_size"])
    if metadata["context_capability"] < minimum:
        raise ValueError("model context capability is below 16K")
    material = {
        "schemaVersion": 2,
        "runtime": "llama.cpp",
        "imageDigest": policy["runtime"]["image"],
        "llamaCppRevision": policy["runtime"]["llama_cpp_revision"],
        "contextSize": minimum,
        "maxPredictedTokens": 768,
        "parallel": 1,
        "threads": 2,
        "threadsBatch": 2,
        "batchSize": 512,
        "ubatchSize": 128,
        "jinja": True,
        "reasoningMode": policy["runtime"]["reasoning_mode"],
        "chatTemplateSource": "embedded_gguf",
        "chatTemplateSha256": metadata["chat_template_sha256"],
        "architecture": metadata["architecture"],
        "artifactSizeBytes": artifact["size_bytes"],
        "resources": resource_class(artifact["size_bytes"], policy),
        "policyChecksum": digest(policy),
    }
    return {"profileId": digest(material), **material}


def build_catalog(artifact: dict, metadata: dict, profile: dict) -> dict:
    return {
        "artifact_uri": artifact["artifact_url"],
        "artifact_sha256": artifact["sha256"],
        "quantization": metadata["quantization"],
        "image": profile["imageDigest"],
        "serving": profile,
    }


def onboarding_id(scope: str, alias: str, artifact: dict, policy: dict) -> str:
    if scope != "recommendation" or not ALIAS_RE.fullmatch(alias):
        raise ValueError("scope or model alias is invalid")
    identity = {key: artifact[key] for key in
                ("repository", "revision", "filename", "sha256", "size_bytes")}
    return "onb-" + digest([scope, alias, identity, digest(policy)])[:32]
