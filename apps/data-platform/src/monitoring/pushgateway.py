from __future__ import annotations

import os
import urllib.error
import urllib.parse
import warnings
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSample:
    name: str
    value: float
    labels: dict[str, str] | None = None


def render_samples(samples: list[MetricSample]) -> str:
    lines: list[str] = []
    typed: set[str] = set()
    for sample in samples:
        if sample.name not in typed:
            typed.add(sample.name)
            lines.append(f"# TYPE {sample.name} gauge")
        labels = sample.labels or {}
        label_text = ""
        if labels:
            rendered = ",".join(f'{key}="{escape_label_value(value)}"' for key, value in sorted(labels.items()))
            label_text = "{" + rendered + "}"
        lines.append(f"{sample.name}{label_text} {float(sample.value)}")
    return "\n".join(lines) + "\n"


def escape_label_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def push_metrics(
    samples: list[MetricSample],
    job: str,
    gateway_url: str | None = None,
    grouping_key: dict[str, str] | None = None,
) -> bool:
    gateway = (gateway_url or os.getenv("PUSHGATEWAY_URL", "")).rstrip("/")
    if not gateway:
        return False
    path = f"{gateway}/metrics/job/{urllib.parse.quote(job, safe='')}"
    for key, value in sorted((grouping_key or {}).items()):
        path += f"/{urllib.parse.quote(str(key), safe='')}/{urllib.parse.quote(str(value), safe='')}"
    request = urllib.request.Request(
        path,
        data=render_samples(samples).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        warnings.warn(f"Failed to push metrics to Pushgateway {gateway}: {exc}", RuntimeWarning)
        return False
