"""Backward-compatible entrypoint for the streaming data generator."""

from streaming.producer import main


if __name__ == "__main__":
    raise SystemExit(main())
