from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jenkins.python.configuration import ROOT, load_components  # noqa: E402

MIGRATION_PATH = re.compile(r"(^|/)(migrations?|alembic|schema)(/|$)", re.IGNORECASE)
DESTRUCTIVE_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN|SCHEMA|DATABASE)|TRUNCATE\s+TABLE|ALTER\s+TABLE\b[^;]*\bDROP\b)",
    re.IGNORECASE | re.DOTALL,
)


def changed_files(base_ref: str = "") -> list[Path]:
    if base_ref:
        command = ["git", "diff", "--name-only", f"{base_ref}...HEAD"]
    else:
        command = ["git", "ls-files"]
    output = subprocess.check_output(command, cwd=ROOT, text=True)
    return [
        ROOT / line
        for line in output.splitlines()
        if line and MIGRATION_PATH.search(line)
    ]


def validate_reversible_manifest(component: str) -> None:
    path = ROOT / "jenkins" / "config" / "migrations" / f"{component}.json"
    if not path.exists():
        raise ValueError(f"reversible component requires migration manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {"up", "verify", "down", "verifyDown", "oldImageCompatibility"}
    missing = required - payload.keys()
    if missing or any(not str(payload.get(field, "")).strip() for field in required):
        raise ValueError(
            f"reversible migration manifest for {component} is missing executable fields: {sorted(missing)}"
        )


def validate(component: str, base_ref: str = "") -> None:
    components = {row["name"]: row for row in load_components()}
    if component not in components:
        raise ValueError(f"unknown component: {component}")
    policy = components[component]["migrationPolicy"]
    candidates = [
        path for path in changed_files(base_ref) if path.exists() and path.is_file()
    ]
    if policy == "none" and candidates:
        names = ", ".join(str(path.relative_to(ROOT)) for path in candidates)
        raise ValueError(
            f"{component} has migrationPolicy=none but migration files changed: {names}"
        )
    for path in candidates:
        if path.suffix.lower() == ".sql" and DESTRUCTIVE_SQL.search(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            raise ValueError(
                f"destructive migration is forbidden by {policy}: {path.relative_to(ROOT)}"
            )
    if policy == "reversible":
        validate_reversible_manifest(component)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enforce component database migration rollback policy."
    )
    parser.add_argument("--component", required=True)
    parser.add_argument("--base-ref", default="")
    args = parser.parse_args()
    validate(args.component, args.base_ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
