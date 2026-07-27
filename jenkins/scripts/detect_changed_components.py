#!/usr/bin/env python3
"""Compatibility CLI for the modular change-detection package."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jenkins.python.change_detection import detector
from jenkins.python.change_detection.detector import *  # noqa: F403
from jenkins.python.change_detection.detector import main


if __name__ == "__main__":
    raise SystemExit(main())
