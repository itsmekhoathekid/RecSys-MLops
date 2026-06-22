from __future__ import annotations


def main() -> int:
    raise SystemExit(
        "Streaming feature jobs are implemented as PyFlink-compatible contracts. "
        "Run them inside the Flink service defined in infra/docker/docker-compose.dataflow.yml."
    )


if __name__ == "__main__":
    main()

