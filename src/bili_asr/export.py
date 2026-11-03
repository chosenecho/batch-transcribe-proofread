"""Export read-only projections of workflow records to JSON or CSV."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .artifact_root import ArtifactRoots
from bili_asr.search_index.readers import extract_transcript_text

#: The standard CSV column order, pinned: a reader parses this header once and
#: relies on the position of every column.  ``video_title`` sits directly after
#: ``title`` because the two are the pair compass **D5** distinguishes — ``title``
#: is the part's own name, ``video_title`` the collection it came from — and a
#: custom column would have sorted the video's title to the very end of the row.
STANDARD_CSV_COLUMNS: tuple[str, ...] = (
    "work_id",
    "bvid",
    "page_index",
    "cid",
    "page_label",
    "title",
    "video_title",
    "status",
    "duration_s",
    "pubdate",
    "source",
    "srt_path",
    "vtt_path",
    "txt_path",
    "md_path",
    "raw_path",
    "audio_path",
)

# Append these after all previously exported columns, including optional text
# and custom columns. The positions of those established columns stay stable.
SOURCE_METADATA_CSV_COLUMNS: tuple[str, ...] = (
    "video_description", "uploader_mid", "uploader_name", "video_tags",
    "video_tags_state", "video_details_observed_at",
)

SENSITIVE_EXPORT_KEYS: frozenset[str] = frozenset(
    {
        "sessdata",
        "cookie",
        "cookies",
        "bili_sessdata",
        "url",
        "signed_url",
        "stream_url",
        "audio_url",
        "video_url",
        "cover_url",
        "api_url",
        "raw_url",
        "play_url",
        "custom_url",
        "traceback",
        "exception",
        "error_trace",
        "stack_trace",
        "token",
        "access_token",
        "secret",
        "password",
        "authorization",
        "auth",
    }
)

_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_url",
    "_cookie",
    "_token",
    "_secret",
    "_trace",
    "_exception",
)

_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)(?:sessdata|access[_-]?token|authorization|cookie|token|signature|sign|deadline)"
        r"\s*(?:=|:)\s*[^\s,;]+"
    ),
    re.compile(r"(?i)https?://[^\s\"\']+(?:sign|deadline|token|auth)[^\s\"\']*"),
    re.compile(r"(?i)Traceback \(most recent call last\):.*", re.DOTALL),
)


def _is_sensitive_key(key: str) -> bool:
    """Return True if the key name indicates sensitive or raw URL data."""
    k = key.lower().strip()
    if k in SENSITIVE_EXPORT_KEYS:
        return True
    if k.endswith(_SENSITIVE_KEY_SUFFIXES):
        return True
    if k.startswith(("cookie", "token", "sessdata", "secret", "auth")):
        return True
    if k.endswith("url"):
        return True
    if "sessdata" in k or "traceback" in k:
        return True
    return False


def _redact_sensitive_text(text: str) -> str:
    """Strip signed URLs, credentials, and tracebacks from string values."""
    redacted = text
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


_DESCRIPTION_URL = re.compile(r"(?i)https?://[^\s\"\'<>]+")
_SENSITIVE_URL_PARAMS = frozenset({
    "sessdata", "access_token", "token", "authorization", "auth", "cookie",
    "signature", "sign", "deadline", "secret", "password", "expires",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "x-goog-signature", "x-goog-credential", "key-pair-id", "wssecret", "w_rid",
})
_DESCRIPTION_CREDENTIALS = re.compile(
    r"(?i)(?<![\w])(?:sessdata|access[_-]?token|authorization|cookie|token|signature|sign|deadline|secret|password)"
    r"\s*(?:=|:)\s*[^\s,;]+"
)


def _redact_description(text: str) -> str:
    """Preserve source prose and ordinary links while filtering credentials.

    This exception belongs to description fields only. URL-valued fields and
    other export strings retain their existing stricter policy.
    """
    def redact_url(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            keys = {key.lower().replace("-", "_") for key, _ in parse_qsl(parsed.query)}
            sensitive_keys = {key.replace("-", "_") for key in _SENSITIVE_URL_PARAMS}
            if (parsed.username is not None or parsed.password is not None
                    or keys & sensitive_keys
                    or host == "bilivideo.com" or host.endswith(".bilivideo.com")):
                return "[redacted]"
        except ValueError:
            return "[redacted]"
        return value

    redacted = _DESCRIPTION_URL.sub(redact_url, text)
    for pattern in (_DESCRIPTION_CREDENTIALS, _SENSITIVE_PATTERNS[2]):
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _safe_contained_relpath(
    root: str | os.PathLike[str] | None,
    path_val: Any,
) -> str | None:
    """Return a normalized archive-root-relative path, or None if path escapes root."""
    if not path_val:
        return None
    path_str = str(path_val).strip()
    if not path_str:
        return None

    # Reject explicit parent directory traversal components
    parts = Path(path_str).parts
    if ".." in parts:
        return None

    if root is not None:
        root_str = os.fspath(root)
        try:
            full = path_str if os.path.isabs(path_str) else os.path.join(root_str, path_str)
            real_full = os.path.realpath(full)
            real_root = os.path.realpath(root_str)
            if os.path.commonpath([real_full, real_root]) == real_root:
                return os.path.relpath(real_full, real_root)
            return None
        except Exception:
            return None
    else:
        # Without root, reject absolute paths
        if os.path.isabs(path_str):
            return None
        return path_str


def _contained_relpath_over_bases(
    bases: tuple[str | os.PathLike[str] | None, ...],
    path_val: Any,
) -> str | None:
    """The first base containing ``path_val``, as a normalized relative path.

    The guard is `_safe_contained_relpath`, asked once per base — contract §5/§6 fix
    that shape as "shared base list, per-family guard".  An empty base list keeps the
    shipped rootless behaviour.
    """
    for base in bases or (None,):
        relative = _safe_contained_relpath(base, path_val)
        if relative is not None:
            return relative
    return None


def _sanitize_value(
    val: Any,
    archive_root: str | os.PathLike[str] | None = None,
    *, field_name: str | None = None,
) -> Any:
    """Recursively sanitize nested dictionaries, lists, and string values."""
    if isinstance(val, dict):
        sanitized_dict: dict[str, Any] = {}
        for k, v in sorted(val.items(), key=lambda item: str(item[0])):
            if field_name == "sourceMetadata" and k == "coverUrl" and val.get("version") == "source-metadata-v2":
                from bili_asr.source_metadata import public_cover_url
                sanitized_dict["coverUrl"] = public_cover_url(v, platform=val.get("platform"), codec_version=2)
                continue
            if _is_sensitive_key(str(k)):
                continue
            child_name = "video_description" if field_name == "sourceMetadata" and k == "description" else str(k)
            sanitized_dict[str(k)] = _sanitize_value(v, archive_root=archive_root, field_name=child_name)
        return sanitized_dict
    elif isinstance(val, list):
        return [_sanitize_value(item, archive_root=archive_root) for item in val]
    elif isinstance(val, str):
        if field_name == "video_description":
            return _redact_description(val)
        if val.startswith(("http://", "https://")):
            return "[redacted]"
        return _redact_sensitive_text(val)
    return val


def sanitize_export_entry(
    entry: dict[str, Any],
    with_text: bool = False,
    archive_root: str | os.PathLike[str] | None = None,
    max_text_length: int | None = None,
    *,
    artifact_roots: ArtifactRoots | None = None,
) -> dict[str, Any]:
    """Return a sanitized record without credentials or signed URLs.

    If with_text is True and a root is available, transcript text is loaded
    and attached under 'transcript_text'. If with_text is False, transcript text
    is excluded.
    Path fields are validated against the roots to prevent path traversal leaks.

    ``artifact_roots`` carries the bases the artifact path fields were written under
    (contract §10, export row): the five transcript paths and audio path are
    products, so a value that
    resolves under only one of the two bases still exports as a normalized relative
    path instead of being stripped to ``""``.  ``None`` keeps the shipped behaviour —
    ``archive_root`` alone, and the rootless mode when that is ``None`` too.
    """
    bases: tuple[str | os.PathLike[str] | None, ...] = (
        artifact_roots.read_bases() if artifact_roots is not None else (archive_root,)
    )
    raw_sanitized: dict[str, Any] = {}
    for key, value in entry.items():
        key_str = str(key)
        if _is_sensitive_key(key_str):
            continue
        # Exclude transcript_text by default unless explicitly requested
        if key_str in ("transcript_text", "text") and not with_text:
            continue

        # Path sanitization for known path keys or keys ending in _path
        if key_str in STANDARD_CSV_COLUMNS and key_str.endswith("_path"):
            safe_rel = _contained_relpath_over_bases(bases, value)
            if safe_rel is not None:
                raw_sanitized[key_str] = safe_rel
            elif value:
                # Path existed but escaped root -> strip to empty
                raw_sanitized[key_str] = ""
            continue
        elif key_str.endswith("_path"):
            safe_rel = _contained_relpath_over_bases(bases, value)
            if safe_rel is not None:
                raw_sanitized[key_str] = safe_rel
            continue

        raw_sanitized[key_str] = _sanitize_value(value, archive_root=archive_root, field_name=key_str)

    if with_text:
        if "transcript_text" not in raw_sanitized:
            text_root = archive_root if archive_root is not None else (
                artifact_roots.archive_root if artifact_roots is not None else None
            )
            if text_root is not None:
                text, _paths = extract_transcript_text(
                    text_root, entry, artifact_roots=artifact_roots
                )
                text = _redact_sensitive_text(text)
                if max_text_length is not None and max_text_length >= 0:
                    text = text[:max_text_length]
                raw_sanitized["transcript_text"] = text
            else:
                raw_sanitized["transcript_text"] = ""
        else:
            text = str(raw_sanitized["transcript_text"])
            text = _redact_sensitive_text(text)
            if max_text_length is not None and max_text_length >= 0:
                text = text[:max_text_length]
            raw_sanitized["transcript_text"] = text

    # Deterministically order the keys: standard columns first, transcript_text, then extra sorted keys
    ordered: dict[str, Any] = {}
    for col in STANDARD_CSV_COLUMNS:
        if col in raw_sanitized:
            ordered[col] = raw_sanitized[col]
    if with_text and "transcript_text" in raw_sanitized:
        ordered["transcript_text"] = raw_sanitized["transcript_text"]
    for k in sorted(raw_sanitized.keys()):
        if k not in ordered and k not in SOURCE_METADATA_CSV_COLUMNS:
            ordered[k] = raw_sanitized[k]
    for k in SOURCE_METADATA_CSV_COLUMNS:
        if k in raw_sanitized:
            ordered[k] = raw_sanitized[k]

    return ordered


def _entry_key_from_dict(entry: dict[str, Any]) -> str:
    work_id = entry.get("work_id")
    if work_id:
        return str(work_id)
    bvid = entry.get("bvid")
    if bvid:
        return str(bvid)
    return str(id(entry))


def export_rows(
    store_or_entries: dict[str, dict[str, Any]] | list[dict[str, Any]],
    status_filter: set[str] | list[str] | None = None,
    with_text: bool = False,
    archive_root: str | os.PathLike[str] | None = None,
    limit: int | None = None,
    max_text_length: int | None = None,
    *,
    artifact_roots: ArtifactRoots | None = None,
) -> list[dict[str, Any]]:
    """Derive sanitized export rows, optionally filtered by workflow status.

    ``artifact_roots`` is the base list the artifact path fields and ``--with-text``
    are resolved against (contract §10); ``None`` keeps the single ``archive_root``
    base this function has always used.
    """
    if isinstance(store_or_entries, dict):
        entries = store_or_entries
        root = archive_root
    elif isinstance(store_or_entries, list):
        entries = {_entry_key_from_dict(item): item for item in store_or_entries}
        root = archive_root
    else:
        entries = {}
        root = archive_root

    filter_set = set(status_filter) if status_filter is not None else None

    def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[str, int, str]:
        key, entry = item
        bvid = str(entry.get("bvid") or key)
        page_idx = entry.get("page_index")
        page_num = (
            int(page_idx)
            if isinstance(page_idx, (int, str)) and str(page_idx).isdigit()
            else 0
        )
        work_id = str(entry.get("work_id") or key)
        return (bvid, page_num, work_id)

    sorted_entries = sorted(entries.items(), key=_sort_key)
    rows: list[dict[str, Any]] = []

    for _key, entry in sorted_entries:
        entry_status = str(entry.get("status") or "")
        if filter_set is not None and entry_status not in filter_set:
            continue
        sanitized = sanitize_export_entry(
            entry,
            with_text=with_text,
            archive_root=root,
            max_text_length=max_text_length,
            artifact_roots=artifact_roots,
        )
        rows.append(sanitized)

    if limit is not None and limit >= 0:
        rows = rows[:limit]

    return rows


def format_json_export(rows: list[dict[str, Any]]) -> str:
    """Serialize exported rows to formatted JSON."""
    return json.dumps(rows, indent=2, ensure_ascii=False)


def format_csv_export(
    rows: list[dict[str, Any]],
    with_text: bool = False,
) -> str:
    """Serialize exported rows to CSV format."""
    seen_keys: set[str] = set()
    for row in rows:
        seen_keys.update(row.keys())

    fieldnames: list[str] = [col for col in STANDARD_CSV_COLUMNS if col in seen_keys or not rows]
    if with_text:
        fieldnames.append("transcript_text")

    # Add remaining custom columns deterministically
    extra_keys = sorted(k for k in seen_keys if k not in fieldnames
                        and k != "transcript_text" and k not in SOURCE_METADATA_CSV_COLUMNS)
    fieldnames.extend(extra_keys)
    fieldnames.extend(SOURCE_METADATA_CSV_COLUMNS)

    # Ensure fieldnames has standard columns if rows is empty
    if not fieldnames:
        fieldnames = list(STANDARD_CSV_COLUMNS)
        if with_text:
            fieldnames.append("transcript_text")

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()

    for row in rows:
        formatted_row: dict[str, Any] = {}
        for k in fieldnames:
            val = row.get(k)
            if val is None:
                formatted_row[k] = ""
            elif isinstance(val, (dict, list)):
                formatted_row[k] = json.dumps(val, ensure_ascii=False, sort_keys=True)
            else:
                formatted_row[k] = str(val)
        writer.writerow(formatted_row)

    return buffer.getvalue()


def export_records(
    archive_root: str | os.PathLike[str],
    fmt: str,
    out_path: str | os.PathLike[str] | None = None,
    status_filter: set[str] | list[str] | None = None,
    with_text: bool = False,
    limit: int | None = None,
    max_text_length: int | None = None,
    *,
    artifact_roots: ArtifactRoots | None = None,
    private: bool = False,
) -> str:
    """Export a read-only projection of workflow records to JSON or CSV."""
    from bili_asr.compliance import compliance_enabled
    if compliance_enabled() and with_text and not private:
        raise ValueError('content-rights: text exports require --private; use publication export for public content')
    fmt_lower = fmt.lower().strip()
    if fmt_lower not in {"json", "csv"}:
        raise ValueError(f"unsupported export format: {fmt!r}; choose 'json' or 'csv'")

    root = os.fspath(archive_root)
    from bili_asr.services.workflow_projection import workflow_has_data, workflow_records

    entries = workflow_records(root, with_text=with_text, artifact_roots=artifact_roots) if workflow_has_data(root) else {}
    rows = export_rows(
        entries,
        status_filter=status_filter,
        with_text=with_text,
        archive_root=archive_root,
        limit=limit,
        max_text_length=max_text_length,
        artifact_roots=artifact_roots,
    )

    if fmt_lower == "json":
        content = format_json_export(rows)
    else:
        content = format_csv_export(rows, with_text=with_text)

    if out_path and str(out_path) != "-":
        out_str = os.fspath(out_path)
        parent = os.path.dirname(out_str)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = out_str + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
                if not content.endswith("\n"):
                    fh.write("\n")
            os.replace(tmp_path, out_str)
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    return content
