"""Readers implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from bili_asr.archive import archive_stem, bundle_relpaths_for_stem
from bili_asr.artifacts import REQUIRED_ARTIFACT_KEYS
from bili_asr.artifact_root import ArtifactRoots
import bili_asr.search_index.common as _dependency_common


def _safe_contained_relpath(root: str, path_str: str) -> str | None:
    """Return a relative path within root, or None if path escapes root."""
    try:
        full = path_str if os.path.isabs(path_str) else os.path.join(root, path_str)
        real_full = os.path.realpath(full)
        real_root = os.path.realpath(root)
        if os.path.commonpath([real_full, real_root]) == real_root:
            return os.path.relpath(real_full, real_root)
    except Exception:
        pass
    return None


def _locate_over_bases(
    bases: tuple[Path, ...], value: str
) -> tuple[str, str] | None:
    """The first base that contains ``value``, with the value made relative to it.

    The containment guard stays per base and unchanged (contract §5/§6); only the
    base list is shared, so a legacy value that resolves under the archive root is
    still described relative to it.
    """
    for base in bases:
        base_str = os.fspath(base)
        relative = _safe_contained_relpath(base_str, value)
        if relative:
            return base_str, relative
    return None


def _existing_path(bases: tuple[Path, ...], relative: str) -> str | None:
    """The first base that actually holds ``relative``, or ``None``.

    Containment is lexical and answers for a base that does not exist, so the base a
    file is *read* from is decided by existence — the same "first hit over the ordered
    bases" rule the bundle and audio probes use (contract §5, D8).
    """
    for base in bases:
        full = os.path.join(os.fspath(base), relative)
        if os.path.isfile(full):
            return full
    return None


def extract_transcript_text(
    root: str | os.PathLike[str],
    entry: dict[str, Any],
    *,
    artifact_roots: ArtifactRoots | None = None,
) -> tuple[str, dict[str, str]]:
    """Load transcript text and gather archive paths for an entry.

    ``artifact_roots`` carries the bases the transcripts live under (contract
    §5/§10, D8): every declared value and every on-disk probe walks ``read_bases()``
    in order, while the returned mapping keeps its shape — relative path strings.
    Each located file is read from the base that actually holds it, so a legacy
    transcript at the archive root is still read there instead of being shadowed by
    the configured base that merely contains its name.
    """
    roots = artifact_roots if artifact_roots is not None else ArtifactRoots.of(root)
    bases = roots.read_bases()
    root_str = os.fspath(root)
    try:
        stem = archive_stem(entry)
    except Exception:
        stem = str(entry.get("bvid") or "")

    paths: dict[str, str] = {}
    located: dict[str, str] = {}
    # Collect paths from entry metadata with containment validation
    for k in REQUIRED_ARTIFACT_KEYS:
        raw_val = entry.get(k)
        if raw_val:
            found = _locate_over_bases(bases, str(raw_val))
            if found:
                base_str, rel = found
                paths[k] = rel
                located[k] = _existing_path(bases, rel) or os.path.join(base_str, rel)

    # If not in entry metadata, probe standard disk locations
    for k, rel in bundle_relpaths_for_stem(stem).items():
        if k in paths:
            continue
        full = _existing_path(bases, rel)
        if full is not None:
            paths[k] = rel
            located[k] = full

    # Load text content
    text = ""
    # 1. Try txt_path
    txt_path = paths.get("txt_path")
    if txt_path:
        full_txt = located.get("txt_path", os.path.join(root_str, txt_path))
        if os.path.isfile(full_txt):
            try:
                with open(full_txt, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
            except OSError:
                pass

    # 2. If no text, try srt_path
    if not text and "srt_path" in paths:
        srt_path = paths["srt_path"]
        full_srt = located.get("srt_path", os.path.join(root_str, srt_path))
        if os.path.isfile(full_srt):
            try:
                with open(full_srt, "r", encoding="utf-8") as fh:
                    srt_lines = []
                    for line in fh:
                        line = line.strip()
                        if not line or line.isdigit() or "-->" in line:
                            continue
                        srt_lines.append(line)
                    text = " ".join(srt_lines)
            except OSError:
                pass

    # 3. If no text, try raw_path or subtitles/raw
    if not text:
        raw_candidates = []
        if "raw_path" in paths:
            raw_candidates.append(located.get("raw_path", os.path.join(root_str, paths["raw_path"])))
        raw_candidates.extend(
            os.path.join(os.fspath(base), "subtitles", "raw", f"{stem}.json")
            for base in bases
        )
        for raw_cand in raw_candidates:
            if os.path.isfile(raw_cand):
                try:
                    with open(raw_cand, "r", encoding="utf-8") as fh:
                        doc = json.load(fh)
                    if not isinstance(doc, dict):
                        continue
                    # A manifest ASR row must not acquire caption text through
                    # this fallback. Legacy caption bodies carry no source, so
                    # only rows with an explicit ASR origin require the guard.
                    if entry.get("source") == "asr" and doc.get("source") != "asr":
                        continue
                    if isinstance(doc.get("body"), list):
                        items, text_key = doc["body"], "content"
                    elif isinstance(doc.get("segments"), list):
                        items, text_key = doc["segments"], "text"
                    else:
                        continue
                    if any(
                        not isinstance(item, dict)
                        or not isinstance(item.get(text_key), str)
                        for item in items
                    ):
                        continue
                    text = " ".join(item[text_key] for item in items if item[text_key].strip())
                    if text:
                        break
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass

    # 4. If no text, try md_path
    if not text and "md_path" in paths:
        md_path = paths["md_path"]
        full_md = located.get("md_path", os.path.join(root_str, md_path))
        if os.path.isfile(full_md):
            try:
                with open(full_md, "r", encoding="utf-8") as fh:
                    md_content = fh.read().strip()
                if md_content.startswith("---"):
                    parts = md_content.split("---", 2)
                    if len(parts) >= 3:
                        md_content = parts[2].strip()
                text = md_content
            except OSError:
                pass

    return _dependency_common._redact_text(text), paths


def _published_md_bases(artifact_roots: ArtifactRoots) -> tuple[Path, ...]:
    """Bases that may hold published per-part markdown transcripts."""
    return artifact_roots.read_bases()


def _parse_published_md_text(path: str) -> str | None:
    """Read one published markdown transcript's text blocks.

    The published projection is not this plan's contract: the parse accepts
    the conventional shapes — fenced code blocks and ``HH:MM:SS``-headed
    sections — and a leading YAML front matter is skipped.  Anything else
    yields ``None`` so the row is simply not indexed from this source.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2]
    lines = content.splitlines()
    blocks: list[str] = []
    in_fence = False
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(stripped)
            continue
        if re.match(r"^\d{1,2}:\d{2}:\d{2}\b", stripped):
            if current:
                blocks.append(" ".join(current))
                current = []
            remainder = stripped[8:].strip()
            if remainder:
                current.append(remainder)
            continue
        if stripped:
            current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    if not blocks:
        return None
    return "\n".join(blocks)
