"""Fence decoder bytes independently of the parent, without opening a database."""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from bili_asr.artifact_fences import object_fence
from bili_asr.artifact_inventory import stream_hash
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage_targets import require_safe_path


@contextmanager
def child_artifact_access(context, audio_path):
    if context is None:
        yield
        return
    if (not isinstance(context, dict) or set(context) != {"archive_root", "artifact_root", "object_id"}
            or not isinstance(context["object_id"], str) or re.fullmatch(r"[0-9a-f]{64}", context["object_id"]) is None):
        raise ValueError("invalid retained artifact process context")
    roots = ArtifactRoots.of(context["archive_root"], context["artifact_root"])
    path = Path(audio_path)
    require_safe_path(path)
    if not path.is_absolute() or not any(path.is_relative_to(base / "audio") for base in roots.read_bases()):
        raise ValueError("retained decoder input is outside the bound artifact roots")
    with object_fence(roots, context["object_id"], exclusive=False):
        with path.open("rb") as stream:
            if stream_hash(stream)[1] != context["object_id"]:
                raise ValueError("retained decoder input differs from its fenced byte identity")
        yield
