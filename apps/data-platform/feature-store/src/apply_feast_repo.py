from __future__ import annotations

from feature_store.feast_registry import apply_feature_repo


def main() -> int:
    result = apply_feature_repo()
    print(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

