"""Change detection package used by Jenkins and local CI proofs."""

from .detector import ClassificationResult, classify, classify_paths

__all__ = ["ClassificationResult", "classify", "classify_paths"]
