"""Download a pinned E5 ONNX revision and create generic dynamic INT8 assets.

This script runs only during image build. Production containers receive the
resulting tokenizer, ``model_quantized.onnx``, and checksum manifest and therefore
need no Hugging Face credentials or Internet access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download
from onnxruntime.quantization import QuantType, quantize_dynamic


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def main() -> int:
    """Export reproducible AVX2-compatible assets and their machine-readable manifest."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    snapshot = Path(
        snapshot_download(
            repo_id=args.model,
            revision=args.revision,
            allow_patterns=[
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
                "sentencepiece.bpe.model",
                "onnx/model.onnx",
            ],
        )
    )
    output.mkdir(parents=True, exist_ok=True)
    for filename in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
    ):
        source = snapshot / filename
        if source.exists():
            shutil.copy2(source, output / filename)
    target = output / "model_quantized.onnx"
    # Dynamic QInt8 uses generic CPU kernels and avoids committing to AVX512/VNNI,
    # so the same image remains valid on the GKE e2 CPU node pools.
    quantize_dynamic(
        model_input=str(snapshot / "onnx" / "model.onnx"),
        model_output=str(target),
        weight_type=QuantType.QInt8,
        per_channel=False,
    )
    manifest = {
        "model": args.model,
        "revision": args.revision,
        "file": target.name,
        "sha256": _sha256(target),
        "quantization": "dynamic_qint8_generic",
        "dimension": 384,
    }
    (output / "model_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
