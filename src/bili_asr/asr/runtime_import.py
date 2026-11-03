"""Shared real model-class import gate; never downloads or constructs models."""

from __future__ import annotations

import json
from typing import Any

from .constants import _INSTALL_HINT
from .errors import ASRDependencyError, ASRRuntimeCompatibilityError


def root_error_type(exc: BaseException) -> str:
    """Keep the root category without exporting third-party messages or paths."""
    visited = set()
    while id(exc) not in visited:
        visited.add(id(exc))
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            break
        exc = cause
    for kind in (ModuleNotFoundError, ImportError, AttributeError, OSError, RuntimeError, ValueError):
        if isinstance(exc, kind):
            return kind.__name__
    return "Exception"


def import_model_classes() -> tuple[Any, Any, Any, Any]:
    """Import the exact runner entrypoints and retain lazy-import failure causes."""
    try:
        import torch
        from transformers import (
            AutoModelForMultimodalLM,
            AutoModelForTokenClassification,
            AutoProcessor,
        )
    except Exception as exc:
        if (isinstance(exc, ModuleNotFoundError) and exc.name in {"torch", "transformers"}
                and exc.__cause__ is None and exc.__context__ is None):
            raise ASRDependencyError(f"Qwen3-ASR support is not installed; run: {_INSTALL_HINT}") from exc
        # The packages are present but a lazy import, DLL or transitive dependency
        # failed. Reinstall advice for a missing extra would conceal the failure.
        from .deployment import installed_versions

        versions = {name: value for name, value in installed_versions().items()
                    if name in {"torch", "transformers", "huggingface-hub", "tokenizers"}}
        raise ASRRuntimeCompatibilityError(
            "Installed ASR runtime cannot import Qwen model classes; "
            f"root_error_type={root_error_type(exc)}; packages={json.dumps(versions, sort_keys=True)}. "
            "Use the locked [asr] graph with the host GPU Torch build and run "
            "check-asr-env --backend cuda|rocm|hcu --probe-gpu before ASR."
        ) from exc
    return torch, AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor
