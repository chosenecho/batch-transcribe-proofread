"""Validate archived transcript evidence before importing it into the store.

The manifest is only a candidate list. A successful adoption requires a complete
bundle with matching hashes, page identity and text on every published surface.
Only complete five-product v2 bundles in the current directory layout are read.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping

from bili_asr.artifacts import BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3, REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY
from bili_asr.cues import segments_to_srt, segments_to_vtt, segments_to_txt
from bili_asr.page_identity import parse_work_id
from bili_asr.storage import TranscriptSegmentRecord
from bili_asr.storage.models import MAX_TIMELINE_MS

_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_BASENAMES = {
    "srt_path": "bundle.srt", "vtt_path": "bundle.vtt", "txt_path": "bundle.txt",
    "md_path": "bundle.md", "raw_path": "bundle.raw.json",
}


class AdoptionRefused(ValueError):
    """A bounded reason why a manifest row cannot supply transcript evidence."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AdoptedTranscript:
    source_kind: str
    language: str
    segments: tuple[TranscriptSegmentRecord, ...]
    model_name: str | None = None
    model_revision: str | None = None
    coverage: Mapping[str, Any] | None = None


def _path_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise AdoptionRefused("unsafe_bundle_path")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise AdoptionRefused("unsafe_bundle_path")
    if PurePosixPath(value).is_absolute() or parts[0] != "transcripts":
        raise AdoptionRefused("unsafe_bundle_path")
    return parts


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_confined(root: Path, relative: str, limit: int = _MAX_ARTIFACT_BYTES) -> bytes:
    """Read a regular, bounded file without accepting linked path components."""
    parts = _path_parts(relative)
    directories: list[int] = []
    fd = -1
    try:
        if os.name == "nt":
            path = root
            for component in (None, *parts):
                if component is not None:
                    path = path / component
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise AdoptionRefused("unsafe_bundle_path")
            expected = path.stat()
            fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        else:
            current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directories.append(current)
            for component in parts[:-1]:
                current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                directories.append(current)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            expected = os.fstat(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _signature(expected) != _signature(before):
            raise AdoptionRefused("unsafe_bundle_path")
        if before.st_size > limit:
            raise AdoptionRefused("bundle_oversized")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise AdoptionRefused("bundle_oversized")
        if _signature(before) != _signature(os.fstat(fd)):
            raise AdoptionRefused("bundle_changed")
        return b"".join(chunks)
    except OSError as exc:
        raise AdoptionRefused("bundle_unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def _marker_path(paths: Mapping[str, str], work_id: str) -> str:
    bvid, page = parse_work_id(work_id)
    stem = f"{bvid}.p{page}"
    if all(paths[key] == f"transcripts/{stem}/{_BASENAMES[key]}" for key in REQUIRED_ARTIFACT_KEYS):
        return f"transcripts/{stem}/.bundle-ready"
    raise AdoptionRefused("bundle_identity_mismatch")


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise AdoptionRefused("bundle_metadata_invalid")
    header, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise AdoptionRefused("bundle_metadata_invalid")
    metadata: dict[str, Any] = {}
    for line in header.split("\n"):
        key, separator, value = line.partition(":")
        if not separator or key in metadata:
            raise AdoptionRefused("bundle_metadata_invalid")
        metadata[key] = json.loads(value)
    return metadata, body


def _nonblank(*values: object) -> str | None:
    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), None)


def _segments(document: object) -> tuple[list[dict[str, Any]], tuple[TranscriptSegmentRecord, ...]]:
    if not isinstance(document, dict):
        raise AdoptionRefused("raw_invalid")
    items = document.get("segments", document.get("body"))
    if not isinstance(items, list) or not items:
        raise AdoptionRefused("raw_invalid")
    rendered: list[dict[str, Any]] = []
    records: list[TranscriptSegmentRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise AdoptionRefused("raw_invalid")
        start = item.get("start", item.get("from"))
        end = item.get("end", item.get("to"))
        text = item.get("text", item.get("content"))
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not isinstance(text, str) or not text.strip()):
            raise AdoptionRefused("raw_invalid")
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise AdoptionRefused("raw_invalid")
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        if end_ms > MAX_TIMELINE_MS:
            raise AdoptionRefused("raw_invalid")
        records.append(TranscriptSegmentRecord(start_ms, end_ms, text))
        rendered.append({"start": start, "end": end, "text": text})
    return rendered, tuple(records)


def read_archived_transcript(
    row: Mapping[str, Any], part: Mapping[str, Any], artifact_root: str | os.PathLike[str],
) -> AdoptedTranscript:
    """Return verified archived content, or refuse it without any store write."""
    try:
        work_id = row.get("work_id")
        if (row.get("status") != "archived" or row.get("unresolved")
                or row.get("excluded_from_page_processing") or not isinstance(work_id, str)):
            raise AdoptionRefused("manifest_identity_unresolved")
        bvid, page = parse_work_id(work_id)
        identity = {"bvid": bvid, "page_index": page, "cid": part["cid"], "work_id": work_id}
        if (part["work_id"] != work_id or any(row.get(key) != value for key, value in identity.items())
                or any(isinstance(row.get(key), bool) for key in ("page_index", "cid"))):
            raise AdoptionRefused("manifest_identity_mismatch")
        paths = {key: row.get(key) for key in REQUIRED_ARTIFACT_KEYS}
        for path in paths.values():
            _path_parts(path)
        marker_path = _marker_path(paths, work_id)
        root = Path(os.path.abspath(artifact_root))
        marker = json.loads(_read_confined(root, marker_path, 8192).decode("ascii"))
        schema = marker.get("schema") if isinstance(marker, dict) else None
        artifacts = marker.get("artifacts") if schema in {BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3} else None
        roles = (*REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY) if schema == BUNDLE_SCHEMA_V3 else REQUIRED_ARTIFACT_KEYS
        if not isinstance(artifacts, dict) or set(artifacts) != set(roles):
            raise AdoptionRefused("bundle_marker_invalid")
        payloads: dict[str, str] = {}
        for key, path in paths.items():
            payload = _read_confined(root, path)
            item = artifacts[key]
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or item["path"] != path or hashlib.sha256(payload).hexdigest() != item["sha256"]):
                raise AdoptionRefused("bundle_digest_mismatch")
            payloads[key] = payload.decode("utf-8").replace("\r\n", "\n")
        if schema == BUNDLE_SCHEMA_V3:
            metadata_path = f"{PurePosixPath(paths['srt_path']).parent.as_posix()}/source-metadata.json"
            metadata_payload = _read_confined(root, metadata_path)
            item = artifacts[SOURCE_METADATA_KEY]
            if (item.get("path") != metadata_path
                    or hashlib.sha256(metadata_payload).hexdigest() != item.get("sha256")):
                raise AdoptionRefused("bundle_digest_mismatch")
        metadata, body = _frontmatter(payloads["md_path"])
        if (any(metadata.get(key) != value for key, value in identity.items())
                or any(isinstance(metadata.get(key), bool) for key in ("page_index", "cid"))):
            raise AdoptionRefused("bundle_identity_mismatch")
        source = metadata.get("source")
        if row.get("source") not in (None, "", source):
            raise AdoptionRefused("bundle_source_mismatch")
        raw = json.loads(payloads["raw_path"])
        rendered, segments = _segments(raw)
        if (payloads["srt_path"] != segments_to_srt(rendered)
                or payloads["vtt_path"] != segments_to_vtt(rendered)
                or payloads["txt_path"] != segments_to_txt(rendered) + "\n"
                or body.strip() != segments_to_txt(rendered).strip()):
            raise AdoptionRefused("bundle_content_mismatch")
        if source in ("asr", "asr-local"):
            if raw.get("source") != source:
                raise AdoptionRefused("bundle_source_mismatch")
            provenance = raw.get("provenance") or {}
            if not isinstance(provenance, dict):
                raise AdoptionRefused("raw_invalid")
            for key in ("model_name", "model_revision", "language"):
                raw_value, published_value = provenance.get(key), metadata.get(f"asr_{key}")
                if raw_value is not None and published_value is not None and raw_value != published_value:
                    raise AdoptionRefused("bundle_provenance_mismatch")
            language = _nonblank(row.get("language"), provenance.get("language"), metadata.get("asr_language")) or "und"
            model = _nonblank(provenance.get("model_name"), metadata.get("asr_model_name")) or "legacy-unknown"
            revision = _nonblank(provenance.get("model_revision"), metadata.get("asr_model_revision"))
            coverage = raw.get("coverage")
            if coverage is not None and not isinstance(coverage, dict):
                raise AdoptionRefused("raw_invalid")
            return AdoptedTranscript("asr-local", language, segments, model, revision, coverage)
        if source not in ("subtitle", "subtitle-ai", "subtitle-cc"):
            raise AdoptionRefused("bundle_source_unknown")
        language = (_nonblank(row.get("sub_lan"), row.get("subtitle_language"), row.get("language"))
                    if source == "subtitle" else _nonblank(row.get("language")))
        if language is None:
            raise AdoptionRefused("caption_language_unknown")
        kind = source if source != "subtitle" else ("subtitle-ai" if language.startswith("ai-") else "subtitle-cc")
        if raw.get("source") not in (None, source):
            raise AdoptionRefused("bundle_source_mismatch")
        return AdoptedTranscript(kind, language, segments)
    except AdoptionRefused:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise AdoptionRefused("bundle_invalid") from exc
