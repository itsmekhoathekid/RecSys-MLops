from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.container_scan_policy import evaluate  # noqa: E402


def test_base_python_refreshes_os_security_packages_before_tooling_install():
    dockerfile = (ROOT / "images/base/recsys-base-python/Dockerfile").read_text()
    assert "apt-get update" in dockerfile
    assert "apt-get upgrade -y --no-install-recommends" in dockerfile
    assert dockerfile.index("apt-get upgrade") < dockerfile.index("apt-get install")


def policy() -> dict:
    return json.loads(
        (ROOT / "jenkins/config/container-scan-policy.json").read_text(encoding="utf-8")
    )


def report(result_type: str, *severities: str) -> dict:
    return {
        "Results": [
            {
                "Target": "image",
                "Type": result_type,
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": f"CVE-test-{index}",
                        "PkgName": "dependency",
                        "Severity": severity,
                    }
                    for index, severity in enumerate(severities)
                ],
            }
        ]
    }


def test_python_and_os_vulnerabilities_remain_blocking():
    rejected, accepted = evaluate(
        "recsys-spark",
        report("python-pkg", "HIGH", "CRITICAL"),
        policy(),
        today=dt.date(2026, 7, 27),
    )
    assert len(rejected) == 2
    assert accepted == {"HIGH": 0, "CRITICAL": 0}


def test_vendor_java_baseline_is_bounded_and_expiring():
    rejected, accepted = evaluate(
        "recsys-flink",
        report("jar", "HIGH", "CRITICAL"),
        policy(),
        today=dt.date(2026, 7, 27),
    )
    assert rejected == []
    assert accepted == {"HIGH": 1, "CRITICAL": 1}

    with pytest.raises(ValueError, match="expired"):
        evaluate(
            "recsys-flink",
            report("jar", "HIGH"),
            policy(),
            today=dt.date(2026, 9, 1),
        )


def test_unlisted_images_cannot_use_vendor_exception():
    rejected, accepted = evaluate(
        "recsys-inference-api",
        report("jar", "CRITICAL"),
        policy(),
        today=dt.date(2026, 7, 27),
    )
    assert len(rejected) == 1
    assert accepted == {"HIGH": 0, "CRITICAL": 0}


def test_mlflow_constrained_cryptography_waiver_is_exact_and_short_lived():
    scan_report = {
        "Results": [
            {
                "Target": "Python",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-69247",
                        "PkgName": "cryptography",
                        "Severity": "HIGH",
                    },
                    {
                        "VulnerabilityID": "CVE-unrelated",
                        "PkgName": "cryptography",
                        "Severity": "HIGH",
                    },
                ],
            }
        ]
    }

    rejected, accepted = evaluate(
        "recsys-drift-retrain",
        scan_report,
        policy(),
        today=dt.date(2026, 8, 4),
    )
    assert [item["id"] for item in rejected] == ["CVE-unrelated"]
    assert accepted == {"HIGH": 1, "CRITICAL": 0}

    with pytest.raises(ValueError, match="expired"):
        evaluate(
            "recsys-drift-retrain",
            scan_report,
            policy(),
            today=dt.date(2026, 8, 15),
        )


def test_airflow_rollback_exception_is_exact_and_short_lived():
    scan_report = {
        "Results": [
            {
                "Target": "Python",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2024-45034",
                        "PkgName": "apache-airflow",
                        "Severity": "HIGH",
                    },
                    {
                        "VulnerabilityID": "CVE-unapproved",
                        "PkgName": "unapproved-library",
                        "Severity": "HIGH",
                    },
                ],
            }
        ]
    }
    rejected, accepted = evaluate(
        "recsys-airflow",
        scan_report,
        policy(),
        today=dt.date(2026, 8, 1),
    )
    assert [item["package"] for item in rejected] == ["unapproved-library"]
    assert accepted == {"HIGH": 1, "CRITICAL": 0}

    with pytest.raises(ValueError, match="expired"):
        evaluate(
            "recsys-airflow",
            scan_report,
            policy(),
            today=dt.date(2026, 8, 15),
        )


def test_exact_package_cve_exception_does_not_allow_other_python_findings():
    scan_report = {
        "Results": [
            {
                "Target": "Python",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-59939",
                        "PkgName": "httplib2",
                        "Severity": "HIGH",
                    },
                    {
                        "VulnerabilityID": "CVE-2026-25087",
                        "PkgName": "pyarrow",
                        "Severity": "HIGH",
                    },
                ],
            }
        ]
    }
    rejected, accepted = evaluate(
        "recsys-flink",
        scan_report,
        policy(),
        today=dt.date(2026, 7, 28),
    )
    assert [item["package"] for item in rejected] == ["pyarrow"]
    assert accepted == {"HIGH": 1, "CRITICAL": 0}


def test_beam_constrained_cryptography_exception_is_exact():
    scan_report = {
        "Results": [
            {
                "Target": "Python",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "GHSA-537c-gmf6-5ccf",
                        "PkgName": "cryptography",
                        "Severity": "HIGH",
                    }
                ],
            }
        ]
    }
    rejected, accepted = evaluate(
        "recsys-flink",
        scan_report,
        policy(),
        today=dt.date(2026, 7, 28),
    )
    assert rejected == []
    assert accepted == {"HIGH": 1, "CRITICAL": 0}


def test_superset_cryptography_exception_does_not_allow_other_packages():
    scan_report = {
        "Results": [
            {
                "Target": "Python",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "GHSA-537c-gmf6-5ccf",
                        "PkgName": "cryptography",
                        "Severity": "HIGH",
                    },
                    {
                        "VulnerabilityID": "CVE-other",
                        "PkgName": "pyOpenSSL",
                        "Severity": "HIGH",
                    },
                ],
            }
        ]
    }
    rejected, accepted = evaluate(
        "recsys-analytics-superset",
        scan_report,
        policy(),
        today=dt.date(2026, 7, 28),
    )
    assert [item["package"] for item in rejected] == ["pyOpenSSL"]
    assert accepted == {"HIGH": 1, "CRITICAL": 0}


def test_training_ray_jar_baseline_is_bounded():
    rejected, accepted = evaluate(
        "recsys-mlops-training",
        report("jar", "HIGH", "HIGH", "HIGH"),
        policy(),
        today=dt.date(2026, 7, 28),
    )
    assert rejected == []
    assert accepted == {"HIGH": 3, "CRITICAL": 0}

    with pytest.raises(ValueError, match="baseline exceeded"):
        evaluate(
            "recsys-mlops-training",
            report("jar", "HIGH", "HIGH", "HIGH", "HIGH", "HIGH"),
            policy(),
            today=dt.date(2026, 7, 28),
        )


def test_kafka_connect_vendor_exception_is_exact():
    scan_report = {
        "Results": [
            {
                "Target": "Java",
                "Type": "jar",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-33117",
                        "PkgName": "com.azure:azure-security-keyvault-keys",
                        "Severity": "CRITICAL",
                    },
                    {
                        "VulnerabilityID": "CVE-unrelated",
                        "PkgName": "unapproved-library",
                        "Severity": "HIGH",
                    },
                ],
            }
        ]
    }
    rejected, accepted = evaluate(
        "recsys-kafka-connect",
        scan_report,
        policy(),
        today=dt.date(2026, 7, 28),
    )
    assert [item["package"] for item in rejected] == ["unapproved-library"]
    assert accepted == {"HIGH": 0, "CRITICAL": 1}
