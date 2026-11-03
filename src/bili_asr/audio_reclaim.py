"""Post-archive audio reclaim: delete a part's audio once transcripts exist.

Filesystem-only module. Never opens sockets, never reads the environment and
never logs credentials: the retention policy is resolved once at the command
boundary (``artifact_root.resolve_keep_audio``) and arrives as ``keep``
(contract §7, D15).
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from .artifact_root import ArtifactRoots
from .path_policy import unlink_confined_audio

_AUDIO_EXTENSIONS = (".m4a", ".flac")


def _audio_dir(archive_root: str) -> str:
    return os.path.join(archive_root, "audio")


def _candidate_paths(entry: Mapping[str, Any]) -> list[str]:
    """Relative audio candidates for one entry, most likely first.

    Root-relative by construction: the same string is validated against each
    base in turn, so the candidates do not depend on which root is in play.
    """
    candidates: list[str] = []
    rel = entry.get("audio_path")
    if rel:
        candidates.append(str(rel))
    bvid = str(entry.get("bvid") or "")
    stem = None
    if bvid and entry.get("work_id") and not entry.get("unresolved"):
        from .archive import archive_stem

        stem = archive_stem(dict(entry))  # type: ignore[arg-type]
    elif bvid:
        stem = bvid
    if stem:
        for ext in _AUDIO_EXTENSIONS:
            # Persisted archive keys use POSIX separators on every host.
            candidates.append(f"audio/{stem}{ext}")
    return candidates


def reclaim_audio(
    archive_root: str | os.PathLike[str],
    entry: Mapping[str, Any],
    *,
    artifact_roots: ArtifactRoots | None = None,
    keep: bool,
) -> bool:
    """Unlink confined audio entries after a row reached `archived`.

    ``keep`` is the retention policy the command boundary resolved: ``True``
    (the default policy) retains the audio for future reprocessing and returns
    immediately — this module reads no environment itself (D15).  When reclaim
    is asked for, the row's candidates are removed under **both** bases, in
    order: "do not keep this row's audio" means the copy, wherever it is.
    """
    if keep:
        return False

    roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(archive_root)
    removed = False
    explicit_value = str(entry.get("audio_path") or "")
    candidates = _candidate_paths(entry)
    # A ``ValueError`` for the recorded value is remembered instead of raised out of the
    # scan: the refusal is a property of the entry at *that* base — a symlink, a
    # non-regular file, a name swapped after validation (``path_policy.py:167-179``) — so
    # it says nothing about the other base's copy, and "do not keep this row's audio"
    # means the copy, wherever it is.  It is reported once neither base could handle the
    # recorded value (the shape-invalid case); both callers treat it as non-fatal.
    explicit_error: ValueError | None = None
    explicit_handled = False
    for base in roots.read_bases():
        seen: set[str] = set()
        for relative in candidates:
            if relative in seen:
                continue
            seen.add(relative)
            try:
                did_remove = unlink_confined_audio(base, relative)
            except ValueError as exc:
                if relative == explicit_value:
                    explicit_error = explicit_error or exc
                continue
            except OSError:
                # This base cannot hold the row's audio at all (it has no
                # `audio/`): move on to the other base rather than aborting the
                # scan.  That is the day-one case of D6 — a freshly configured
                # root beside an archive that still holds the row's copy.
                break
            if relative == explicit_value:
                explicit_handled = True
            if did_remove:
                removed = True
    if explicit_error is not None and not explicit_handled:
        raise explicit_error
    return removed
