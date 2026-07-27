from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def write_report(path: Path, component: str, status: int, message: str) -> None:
    suite = ET.Element(
        "testsuite",
        name=f"gcp-{component}",
        tests="1",
        failures="1" if status else "0",
        errors="0",
    )
    case = ET.SubElement(
        suite,
        "testcase",
        classname="jenkins.gcp.production",
        name=f"{component}-production-smoke",
    )
    if status:
        failure = ET.SubElement(case, "failure", message=message or "production smoke failed")
        failure.text = message or f"component test exited with status {status}"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--status", type=int, required=True)
    parser.add_argument("--message", default="")
    args = parser.parse_args()
    write_report(args.path, args.component, args.status, args.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
