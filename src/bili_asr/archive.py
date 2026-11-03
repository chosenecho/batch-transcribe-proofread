"""Archive writers for timestamped transcript outputs."""

from __future__ import annotations

import hashlib
import errno
import json
import math
import os
import secrets
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ContextManager, Mapping

from .cues import segments_to_srt, segments_to_vtt, segments_to_txt
from .artifacts import (
    BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3, BUNDLE_MARKER_NAME,
    BUNDLE_BASENAMES as _BUNDLE_BASENAMES,
    BUNDLE_BASENAMES_V3 as _BUNDLE_BASENAMES_V3,
    REQUIRED_ARTIFACT_KEYS as _REQUIRED_ARTIFACT_KEYS,
    SOURCE_METADATA_KEY as _SOURCE_METADATA_KEY,
    V3_ARTIFACT_KEYS as _V3_ARTIFACT_KEYS,
    owns_bundle_paths as _owned_bundle_parts,
)
from .artifact_root import ArtifactRoots
from .page_identity import artifact_stem, page_identity, page_query_index

# Bundle names and ownership rules are shared by writers, snapshots and readers.
# The private aliases above preserve historical imports while artifacts owns
# the public contract.
_MARKER_MAX_BYTES = 8192
_BUNDLE_LOCKS: dict[str, threading.RLock] = {}
_BUNDLE_LOCKS_GUARD = threading.Lock()


class _InvalidArchiveBundle(OSError):
    """An observed malformed artifact, rather than an unreadable one."""


def _bundle_lock(root: Path) -> threading.RLock:
    with _BUNDLE_LOCKS_GUARD:
        return _BUNDLE_LOCKS.setdefault(os.fspath(root), threading.RLock())


# ``_safe_name`` used to sanitise a title for the markdown's file name
# (``{pubdate}_{stem}_{safe_title}.md``).  Shape A took the title out of every
# path -- the directory is the stem and the files are fixed basenames -- so the
# rule has no caller and is gone rather than left as a decoy.  A title now reaches
# a path only as frontmatter content, where filesystem metacharacters are inert.


def archive_stem(entry: dict[str, Any]) -> str:
    if entry.get("platform", "bilibili") != "bilibili":
        from bili_asr.platform_identity import ContentRef
        from bili_asr.source_identity import artifact_stem as source_artifact_stem
        return source_artifact_stem(ContentRef(entry["platform"], entry["external_video_id"], int(entry["page_index"])))
    bvid = str(entry["bvid"])
    if entry.get("unresolved") or not entry.get("work_id") or entry.get("cid") is None:
        return bvid
    return artifact_stem(page_identity(bvid, int(entry.get("page_index") or 0), int(entry["cid"]), page_label=str(entry.get("page_label") or "")))


def archive_url(entry: dict[str, Any]) -> str:
    if entry.get("platform", "bilibili") != "bilibili":
        from bili_asr.platform_identity import ContentRef
        from bili_asr.source_identity import source_url
        return source_url(ContentRef(entry["platform"], entry["external_video_id"], int(entry["page_index"])))
    url = f"https://www.bilibili.com/video/{entry['bvid']}"
    if not entry.get("unresolved") and entry.get("work_id") and int(entry.get("page_index") or 0) > 0:
        url += f"?p={page_query_index(int(entry['page_index']))}"
    return url


def bundle_marker_path(path: str | os.PathLike[str]) -> Path:
    """The completion marker for the bundle ``path`` belongs to (shape A).

    The marker is a fixed name **inside the work's own directory**, sibling to
    the five artifacts, so it no longer repeats the artifact's name.  ``path`` is
    any member of the bundle (``write_archive`` and every probe pass the srt).
    """
    return Path(os.fspath(path)).parent / BUNDLE_MARKER_NAME


def _component_names(relative: str | os.PathLike[str]) -> tuple[str, ...] | None:
    try:
        path = Path(os.fspath(relative))
    except (TypeError, ValueError):
        return None
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.parts


def _open_dir(parent_fd: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    if create:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(name, flags, dir_fd=parent_fd)


def _open_transcripts_dir(root: Path) -> int:
    """Open (creating) the single ``transcripts`` directory below ``root``.

    Shape A keeps **one** directory level: every work owns
    ``transcripts/{stem}/`` and the five artifacts are fixed names inside it, so
    there are no per-kind directories to open.
    """
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    try:
        return _open_dir(root_fd, "transcripts", create=True)
    finally:
        os.close(root_fd)


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory_fd)
    try:
        os.write(fd, content)
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _write_staged_path(path: Path, content: bytes) -> None:
    """Durably stage a Windows artifact before entering the publication guard."""
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _require_regular_target(
    name: str | os.PathLike[str], *, dir_fd: int | None = None,
    label: str = "archive target",
) -> None:
    """Refuse visible links/nonregular targets before changing publication state."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{label} is not a regular file")


def _replace_at(stage_fd: int, stage_name: str, target_fd: int, target_name: str) -> None:
    _require_regular_target(target_name, dir_fd=target_fd)
    os.replace(stage_name, target_name, src_dir_fd=stage_fd, dst_dir_fd=target_fd)
    _fsync_fd(target_fd)


def _marker_payload(finals: Mapping[str, Path], root: Path, contents: Mapping[str, bytes], *, schema: str) -> bytes:
    artifacts = {
        key: {"path": finals[key].relative_to(root).as_posix(), "sha256": hashlib.sha256(contents[key]).hexdigest()}
        for key in contents
    }
    return (json.dumps({"schema": schema, "artifacts": artifacts}, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _bundle_marker_keys(document: object) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(document, dict):
        return None
    schema = document.get("schema")
    if schema == BUNDLE_SCHEMA:
        return schema, _REQUIRED_ARTIFACT_KEYS
    if schema == BUNDLE_SCHEMA_V3:
        return schema, _V3_ARTIFACT_KEYS
    return None


def _declared_v3_metadata_path(paths: Mapping[str, str]) -> str:
    return f"{PurePosixPath(paths['srt_path']).parent.as_posix()}/{_BUNDLE_BASENAMES_V3[_SOURCE_METADATA_KEY]}"


def _read_fd(fd: int, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise _InvalidArchiveBundle("oversized archive file")


def _read_regular_at(
    directory_fd: int | None, name: str | os.PathLike[str], limit: int
) -> bytes:
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _InvalidArchiveBundle("archive artifact is not regular")
        return _read_fd(fd, limit)
    finally:
        os.close(fd)


def _regular_digest(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> str:
    """Hash one regular artifact with bounded memory and reject concurrent edits."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(path, flags, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _InvalidArchiveBundle("archive artifact is not regular")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > before.st_size:
                raise OSError("archive artifact changed during verification")
            digest.update(chunk)
        after = os.fstat(fd)
        fingerprint = lambda info: (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        )
        if size != before.st_size or fingerprint(before) != fingerprint(after):
            raise OSError("archive artifact changed during verification")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _open_declared(root: Path, relative: str) -> tuple[int, str] | None:
    parts = _component_names(relative)
    if not parts or len(parts) < 2:
        return None
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    current = root_fd
    try:
        for part in parts[:-1]:
            nxt = _open_dir(current, part)
            os.close(current)
            current = nxt
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def archive_bundle_complete(
    archive_root: str | os.PathLike[str],
    paths: Mapping[str, str],
    *,
    require_readable: bool = False,
) -> bool:
    """Verify a bundle; optionally refuse an inconclusive filesystem read.

    Readers retain the boolean contract. Publishers use ``require_readable``
    before deciding to replace a bundle: missing paths and observed corruption
    return ``False``, while permission, device and concurrent-read failures
    raise ``OSError`` so an unverified existing bundle is preserved.
    """
    if os.name == "nt":
        try:
            root = ArtifactRoots.of(archive_root).archive_root
            if set(paths) != set(_REQUIRED_ARTIFACT_KEYS) or not _owned_bundle_parts(paths):
                return False
            marker = root / os.path.dirname(paths["srt_path"]) / BUNDLE_MARKER_NAME
            document = json.loads(
                _read_regular_at(None, marker, limit=_MARKER_MAX_BYTES).decode("ascii")
            )
            declaration = _bundle_marker_keys(document)
            artifacts = document.get("artifacts") if declaration is not None else None
            keys = declaration[1] if declaration is not None else ()
            if not isinstance(artifacts, dict) or set(artifacts) != set(keys):
                return False
            for key in keys:
                item = artifacts[key]
                expected_path = (paths[key] if key in paths else _declared_v3_metadata_path(paths)).replace(os.sep, "/")
                target = root / expected_path
                if not isinstance(item, dict) or set(item) != {"path", "sha256"} or item.get("path") != expected_path or _regular_digest(target) != item.get("sha256"):
                    return False
            return True
        except OSError as exc:
            if require_readable and not _repairable_bundle_error(exc):
                raise
            return False
        except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False
    try:
        root = _lexical_archive_root(archive_root)
        if set(paths) != set(_REQUIRED_ARTIFACT_KEYS) or any(not isinstance(paths[key], str) for key in _REQUIRED_ARTIFACT_KEYS):
            return False
        if not _owned_bundle_parts(paths):
            return False
        with _bundle_lock(root):
            opened = {}
            marker_item = None
            try:
                for key in _REQUIRED_ARTIFACT_KEYS:
                    item = _open_declared(root, paths[key])
                    if item is None:
                        return False
                    opened[key] = item
                # Shape A: the marker is a fixed name inside the bundle's own
                # directory, sibling to the five artifacts -- not a suffix on the
                # srt path as it was under the four-kind-dir shape.
                marker_rel = os.path.join(os.path.dirname(paths["srt_path"]), BUNDLE_MARKER_NAME)
                marker_item = _open_declared(root, marker_rel)
                if marker_item is None:
                    return False
                document = json.loads(_read_regular_at(*marker_item, limit=_MARKER_MAX_BYTES).decode("ascii"))
                declaration = _bundle_marker_keys(document)
                artifacts = document.get("artifacts") if declaration is not None else None
                keys = declaration[1] if declaration is not None else ()
                if not isinstance(artifacts, dict) or set(artifacts) != set(keys):
                    return False
                if _SOURCE_METADATA_KEY in keys:
                    metadata_path = _declared_v3_metadata_path(paths)
                    metadata_item = _open_declared(root, metadata_path)
                    if metadata_item is None:
                        return False
                    opened[_SOURCE_METADATA_KEY] = metadata_item
                for key in keys:
                    item = artifacts[key]
                    expected_path = paths[key] if key in paths else _declared_v3_metadata_path(paths)
                    if not isinstance(item, dict) or set(item) != {"path", "sha256"} or item["path"] != expected_path or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64:
                        return False
                    directory_fd, name = opened[key]
                    if _regular_digest(name, dir_fd=directory_fd) != item["sha256"]:
                        return False
                return True
            finally:
                for fd, _name in opened.values():
                    os.close(fd)
                if marker_item is not None:
                    os.close(marker_item[0])
    except OSError as exc:
        if require_readable and not _repairable_bundle_error(exc):
            raise
        return False
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _repairable_bundle_error(error: OSError) -> bool:
    return isinstance(error, _InvalidArchiveBundle) or error.errno in {
        errno.ENOENT, errno.ENOTDIR, errno.ELOOP,
    }


def _invalidate_marker(directory_fd: int, marker_name: str) -> None:
    try:
        info = os.stat(marker_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError("archive bundle marker is not a regular file")
    os.unlink(marker_name, dir_fd=directory_fd)
    _fsync_fd(directory_fd)


def _create_bundle_stage(transcripts_fd: int) -> tuple[str, int]:
    """Create an unpredictable staging directory for one publication.

    A fixed staging name lets a hard-terminated publisher strand a directory
    that blocks every later publication. A per-attempt name keeps that stale
    state isolated while retaining descriptor-relative confinement.
    """
    for _ in range(8):
        name = f".archive-bundle-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=transcripts_fd)
        except FileExistsError:
            continue
        try:
            return name, _open_dir(transcripts_fd, name)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=transcripts_fd)
            except OSError:
                pass
            raise
    raise OSError("unable to allocate archive staging directory")


@contextmanager
def _publication_guard(guard, invalidate):
    if guard is not None:
        # The owner must call invalidate before releasing its serialization
        # lock, including failures raised while exiting the context.
        with guard(invalidate):
            yield
    else:
        try:
            yield
        except BaseException:
            invalidate()
            raise


def _publish_bundle(
    root: Path,
    finals: Mapping[str, Path],
    contents: Mapping[str, bytes],
    *,
    before_replace: Callable[[], None] | None = None,
    publication_guard: Callable[[Callable[[], None]], ContextManager[Any]] | None = None,
) -> None:
    """Publish the selected bundle data members with a complete marker.

    Encoding, hashes and staging happen outside ``publication_guard``. The
    optional guard serializes every final replacement and marker commit with
    cancellation; ``before_replace`` still checks each replacement. A failure
    after publication begins invalidates the marker, including a guard's failed
    exit. A supplied guard receives that cleanup callback and must invoke it
    before releasing its lock on failure. The marker makes an interrupted group
    of file replacements invisible.
    """
    if os.name == "nt":
        with _bundle_lock(root):
            work = finals["srt_path"].parent
            work.parent.mkdir(parents=True, exist_ok=True)
            work.mkdir(mode=0o700, exist_ok=True)
            for target in finals.values():
                _require_regular_target(target)
            _require_regular_target(work / BUNDLE_MARKER_NAME, label="archive bundle marker")
            marker = work / BUNDLE_MARKER_NAME
            staged: dict[Path, Path] = {}
            publication_started = False

            def invalidate():
                if publication_started:
                    marker.unlink(missing_ok=True)

            try:
                for key in contents:
                    target = finals[key]
                    temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
                    staged[target] = temporary
                    _write_staged_path(temporary, contents[key])
                temporary = work / f".{BUNDLE_MARKER_NAME}.{secrets.token_hex(16)}.tmp"
                staged[marker] = temporary
                _write_staged_path(temporary, _marker_payload(
                    finals, root, contents,
                    schema=BUNDLE_SCHEMA_V3 if _SOURCE_METADATA_KEY in contents else BUNDLE_SCHEMA,
                ))
                with _publication_guard(publication_guard, invalidate):
                    if before_replace is not None:
                        before_replace()
                    publication_started = True
                    marker.unlink(missing_ok=True)
                    for target, temporary in staged.items():
                        if before_replace is not None:
                            before_replace()
                        os.replace(temporary, target)
            finally:
                for temporary in staged.values():
                    temporary.unlink(missing_ok=True)
        return
    with _bundle_lock(root):
        transcripts_fd = _open_transcripts_dir(root)
        stage_fd = None
        stage_name = ""
        work_name = finals["srt_path"].parent.name
        work_fd = None
        names = {key: finals[key].name for key in contents}
        marker_name = BUNDLE_MARKER_NAME
        publication_started = False

        def invalidate():
            if publication_started and work_fd is not None:
                _invalidate_marker(work_fd, marker_name)

        try:
            stage_name, stage_fd = _create_bundle_stage(transcripts_fd)
            for key in contents:
                _write_at(stage_fd, names[key], contents[key])
            _write_at(stage_fd, marker_name, _marker_payload(
                finals, root, contents,
                schema=BUNDLE_SCHEMA_V3 if _SOURCE_METADATA_KEY in contents else BUNDLE_SCHEMA,
            ))
            _fsync_fd(stage_fd)

            # The work directory is created once and reused across republishes.
            try:
                os.mkdir(work_name, 0o700, dir_fd=transcripts_fd)
            except FileExistsError:
                pass
            work_fd = _open_dir(transcripts_fd, work_name)
            for name in names.values():
                _require_regular_target(name, dir_fd=work_fd)
            with _publication_guard(publication_guard, invalidate):
                if before_replace is not None:
                    before_replace()
                publication_started = True
                _invalidate_marker(work_fd, marker_name)
                for key in contents:
                    if before_replace is not None:
                        before_replace()
                    _replace_at(stage_fd, names[key], work_fd, names[key])
                if before_replace is not None:
                    before_replace()
                _replace_at(stage_fd, marker_name, work_fd, marker_name)
                _fsync_fd(work_fd)
                _fsync_fd(transcripts_fd)
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: OSError | None = None

            def record_cleanup_error(error: OSError) -> None:
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error

            if stage_fd is not None:
                for name in (*names.values(), marker_name):
                    try:
                        os.unlink(name, dir_fd=stage_fd)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        record_cleanup_error(error)
                try:
                    os.close(stage_fd)
                except OSError as error:
                    record_cleanup_error(error)
            if stage_name:
                try:
                    os.rmdir(stage_name, dir_fd=transcripts_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    record_cleanup_error(error)
            if work_fd is not None:
                try:
                    os.close(work_fd)
                except OSError as error:
                    record_cleanup_error(error)
            try:
                os.close(transcripts_fd)
            except OSError as error:
                record_cleanup_error(error)
            if active_error is None and cleanup_error is not None:
                raise cleanup_error

def _lexical_archive_root(archive_root: str | os.PathLike[str]) -> Path:
    root = ArtifactRoots.of(archive_root).archive_root
    if os.name == "nt":
        if root.is_symlink() or not root.is_dir():
            raise OSError("archive publication path is unsafe")
        return root
    fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("archive publication path is unsafe")
    finally:
        os.close(fd)
    return root


#: A cue at or below this mean token score is worth a second look.
LOW_CONFIDENCE = 0.4

#: Two cue spans this close or closer are one captured stretch of audio.
#:
#: The model's result carries no VAD boundary list, so the transcript's own cue
#: intervals are the only capture evidence an artefact has; a cue boundary is a
#: punctuation or 60-character decision, not a capture boundary, so the spans
#: are merged back across pauses the shaper itself tolerates.  The value is
#: ``asr._CUE_MAX_GAP_SECONDS`` — the shaper's own pause threshold.
#:
#: The comparison is ``<=``, one step *wider* than the shaper's, and that is the
#: deliberate reading: the shaper splits at ``gap >= 1.0`` while this merges at
#: ``gap <= 1.0``, so a pause of exactly the threshold is two cues in the
#: transcript and one stretch here.  A 1.0 s pause is not a capture hole, and
#: for a *capture* estimate the gap has to be strictly larger than the shaper's
#: own tolerance before it counts as lost audio.  The boundary is pinned in both
#: directions by ``test_the_shaper_and_the_merger_meet_at_the_threshold_from_opposite_sides``.
#:
#: Declared here rather than imported: the stated cross-layer rule is that
#: ``subtitles``/``audio``/``asr``/``archive`` never import each other, only
#: ``cli`` composing them (asr-archive-cli.md L61).  The two pure formatters
#: ``archive.py`` already imports from ``asr`` are the existing exception, so a
#: new coupling would widen it; the coupling is asserted by
#: ``test_capture_gap_seconds_follows_the_cue_shaper_threshold`` instead.
CAPTURE_GAP_SECONDS = 1.0


def _confidence_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize the model's own token confidence for one transcript.

    The values are the model's, not a judgement: they make quality measurable
    from the artefact alone, without a human reference transcript.

    ``asr_low_confidence_cues`` and ``asr_low_confidence_at`` are two
    renderings of **one** filtered list, so ``len(asr_low_confidence_at) ==
    asr_low_confidence_cues`` holds by construction rather than by test: a cue
    cannot be counted without also being located.  The locations are the low
    cues' start seconds, ascending, rounded to 3 decimals like
    ``asr_mean_confidence`` and with duplicates kept — one entry per counted
    cue, so a reader can recompute the list from ``raw.json`` exactly.

    Both keys are emitted together whenever the transcript carries any score,
    including ``0`` and ``[]`` when nothing is at or below
    :data:`LOW_CONFIDENCE`; when it carries no score neither is emitted, which
    is this function's existing rule for the count.

    Reading ``start`` adds no new way to fail a row — transcript by transcript:
    nothing that published without this read fails with it, and nothing that
    failed without it publishes with it.  The read is the lookup ``s["start"]``,
    ``float`` of it, then ``round(..., 3)``, and rounding to 3 decimals cannot
    fail once ``float`` has returned — ``NaN`` and ``inf`` included — so the
    read fails exactly when that lookup or that ``float`` does.
    ``segments_to_srt`` already applies both to the same field before anything
    is published (``segment['start']``, then ``_fmt_srt_time``'s opening
    ``float(seconds)``), so an absent, non-numeric, ``None`` or
    out-of-float-range start still aborts the row and reaches no
    ``_publish_bundle``.  ``NaN``/``inf`` pass the read and are stopped one step
    *later*, by ``_fmt_srt_time``'s ``float(seconds) * 1000``, which still
    precedes publication: no path can publish them.

    The exception **type** is deliberately not part of that guarantee, and is
    not claimed: this summary reads only the **low** cues, in filtered order,
    while the formatter reads every cue in document order — and because
    :func:`write_archive` assembles frontmatter first, this read now runs
    first.  Given two unreadable starts of different kinds, the row fails on
    whichever one this read reaches first, so a low ``"nope"`` sitting behind a
    non-low *missing* ``start`` raises ``ValueError`` here where the formatter
    alone raised ``KeyError``.  The row still fails and still publishes nothing;
    only the stage's bounded error summary can
    change, which is why no caller may branch on the type.
    """

    scores = [float(s["confidence"]) for s in segments
              if isinstance(s, dict) and isinstance(s.get("confidence"), (int, float))]
    if not scores:
        return {}
    low = [s for s in segments
           if isinstance(s, dict) and isinstance(s.get("confidence"), (int, float))
           and float(s["confidence"]) <= LOW_CONFIDENCE]
    return {
        "asr_mean_confidence": round(sum(scores) / len(scores), 3),
        "asr_low_confidence_cues": len(low),
        "asr_low_confidence_at": sorted(round(float(s["start"]), 3) for s in low),
    }


def _merged_cue_spans(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """The transcript's cue intervals with adjacent ones fused into one span.

    Touching, overlapping and cues separated by **at or below**
    :data:`CAPTURE_GAP_SECONDS` become a single span, which is what makes the
    result a *capture* estimate rather than a punctuation census.  The boundary
    is inclusive on purpose and is one step wider than the shaper's own split
    rule, so a pause of exactly the threshold is one stretch here rather than a
    reported hole.  Non-finite and reversed intervals are skipped: they cannot
    describe captured audio, and the quality checker already names them
    ``malformed``/``out_of_range``.

    **An interval of zero length (``end == start``) is skipped too.**  The rule
    is published here because A5 lets a reader recompute the capture facts from
    ``raw.json`` alone, and a zero-length cue is invisible audio either way it
    is read: counted, it inflates ``asr_vad_segments`` by a span that describes
    no captured stretch, and merged, it can bridge two real spans into one and
    inflate ``asr_vad_captured_s``.  Because the seconds of a zero-length span
    are ``0.0``, skipping it leaves the summed duration identical to counting
    it, so a recomputation that skips zero-length intervals reproduces every
    published value exactly.
    """

    spans: list[tuple[float, float]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end = segment.get("start"), segment.get("end")
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start, end = float(start), float(end)
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        spans.append((start, end))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] <= CAPTURE_GAP_SECONDS:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _capture_summary(segments: list[dict[str, Any]], duration_s: Any) -> dict[str, Any]:
    """Record how much audio the VAD captured, as the cues testify.

    ``asr_vad_segments`` and ``asr_vad_captured_s`` describe the merged cue
    spans; the ratio divides them by the row's own ``duration_s`` and is clamped
    to ``[0, 1]`` because a ratio outside it is not a proportion of anything.
    The seconds stay **unclamped**, so a duration/cue contradiction remains
    visible in the artefact rather than being smoothed away here.

    The ratio divides the **published** ``asr_vad_captured_s`` — the 3-decimal
    value from the key above it, not the exact float sum — so a reader holding
    only the artefact reproduces it exactly.

    The ratio is omitted when ``duration_s`` is not positive and finite — a
    proportion of an unknown total is not a fact — or when it is a number too
    large to divide by, which the same rule covers.  The two absolute keys are
    still emitted, and an empty transcript legitimately reports zero of both.
    """

    merged = _merged_cue_spans(segments)
    captured_s = round(sum((end - start for start, end in merged), 0.0), 3)
    summary: dict[str, Any] = {
        "asr_vad_segments": len(merged),
        "asr_vad_captured_s": captured_s,
    }
    duration = duration_s
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return summary
    try:
        duration = float(duration)
    except OverflowError:
        # Arbitrary-precision ints beyond float range: an unusable denominator,
        # so the ratio is omitted rather than allowed to abort publication.
        return summary
    if not math.isfinite(duration) or duration <= 0:
        return summary
    summary["asr_vad_captured_ratio"] = round(min(1.0, max(0.0, captured_s / duration)), 3)
    return summary


def bundle_dir_for_stem(root: str | os.PathLike[str], stem: str) -> Path:
    """The one directory a work's bundle occupies (shape A)."""
    return Path(os.fspath(root)) / "transcripts" / stem


def bundle_paths_for_stem(root: str | os.PathLike[str], stem: str) -> dict[str, Path]:
    """The five artifact paths for a known ``stem``.

    This is the layout's single authority: a reader that holds only a stem (the
    integrity, quality, search and proofread probes all do) asks here instead of
    restating the shape, which is what stops the layout from being written down
    in twenty places.
    """
    directory = bundle_dir_for_stem(root, stem)
    return {key: directory / _BUNDLE_BASENAMES[key] for key in _REQUIRED_ARTIFACT_KEYS}


def bundle_relpaths_for_stem(stem: str) -> dict[str, str]:
    """The five artifact paths for a stem as root-relative POSIX strings.

    The string-shaped siblings of :func:`bundle_paths_for_stem`: probes that
    compare against a manifest's recorded relative path (and so never build a
    ``Path``) ask here, so the layout still has one authority.
    """
    return {
        key: value.as_posix()
        for key, value in bundle_paths_for_stem("", stem).items()
    }


def bundle_paths(root: str | os.PathLike[str], entry: dict[str, Any]) -> dict[str, Path]:
    """Return the five artifact paths one entry's bundle occupies below ``root``.

    ``transcripts/{stem}/{bundle.srt,bundle.vtt,bundle.txt,bundle.md,bundle.raw.json}``
    uses one directory per work with five fixed names. The stem
    is ``archive_stem(entry)``, so an unresolved or bare-``bvid`` row keeps its
    bare-``bvid`` directory.  ``root`` is used exactly as given, so a relative
    root yields relative paths; ``write_archive`` passes the root it has already
    resolved lexically.

    The markdown file is deliberately **not** named after the pubdate and title
    any more.  That was the defect this shape removes: the md was the one file of
    the four whose name moved when the title or the pubdate moved, so a republish
    orphaned the previous one while its three siblings stayed put.
    """
    return bundle_paths_for_stem(root, archive_stem(entry))

def characters_for(segments: list[dict[str, Any]], characters: Any) -> dict[str, Any]:
    """Return the ``characters`` block to publish for ``segments``, or raise.

    The block is the **character-level record the ASR boundary produced** — the instants the
    forced aligner gave each character, which the cue rules otherwise throw away.  It is passed
    through rather than recomputed: nothing here can re-derive it, and a fabricated one would be
    worse than none.

    What this function *is* is the integrity gate the plan makes a product requirement
    (``characters.text`` must equal ``"".join(s["text"] for s in segments)``): the two are
    independent statements about the same transcript, one character-granular and one cue-granular,
    and a writer that publishes them disagreeing has published a claim it cannot support.  So a
    mismatch **refuses the write** instead of recording the inconsistency.

    The shape is checked here too — the three arrays are one instant per character of ``text``, so
    a record whose arrays disagree in length describes a transcript it cannot support and is refused
    the same way.  A record that passes both checks is returned as a copy, so the published block
    cannot be mutated by whoever still holds the runner's own record.
    """

    if characters is None:
        return {}
    if not isinstance(characters, dict):
        raise ValueError("characters must be a mapping")
    text = characters.get("text")
    starts = characters.get("starts")
    ends = characters.get("ends")
    if not isinstance(text, str) or not isinstance(starts, list) or not isinstance(ends, list):
        raise ValueError("characters must carry text, starts and ends")
    joined = "".join(str(segment.get("text", "")) for segment in segments)
    if text != joined:
        differing = next(
            (
                position
                for position, (left, right) in enumerate(zip(text, joined))
                if left != right
            ),
            min(len(text), len(joined)),
        )
        raise ValueError(
            "characters.text does not match the segments it is published beside: "
            f"first difference at character {differing} "
            f"({len(text)} characters vs {len(joined)})"
        )
    # An all-empty record describes nothing, so it means "no record" rather than "a record of
    # length zero".  Publishing it would set ``schema: archive-raw-v2`` on a bundle that carries no
    # character timings, and a consumer switching on that marker would treat a zero-character
    # transcription as character-annotated.  ``None`` and an empty record must therefore agree.
    # (Found by L2 review: the empty record was truthy, so the two shapes disagreed.)
    if not text and not starts and not ends:
        return {}
    if len(starts) != len(text) or len(ends) != len(text):
        raise ValueError(
            "characters must carry one instant per character "
            f"({len(text)} characters, {len(starts)} starts, {len(ends)} ends)"
        )
    # The plan states three shape invariants; the length check above is only the
    # first.  A record whose instants run backwards, or whose interval is inverted,
    # describes a transcript that never happened — and a re-tiler indexing it would
    # emit cues that go back in time.  Both are refused rather than published.
    # (Found by L2 review: only the length invariant was enforced.)
    for position, (start, end) in enumerate(zip(starts, ends)):
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            raise ValueError(
                f"characters instants must be numbers: position {position} carries "
                f"{type(start).__name__} / {type(end).__name__}"
            )
        # Integers are finite without a float conversion (which could overflow).
        # NaN also defeats both the interval and monotonicity comparisons below,
        # and JSON's default encoder would publish it as a non-standard number.
        if any(isinstance(value, float) and not math.isfinite(value) for value in (start, end)):
            raise ValueError(
                "characters instants must be finite: "
                f"position {position} starts at {start!r} and ends at {end!r}"
            )
        if start < 0 or end < 0:
            raise ValueError(
                "characters instants must not be negative: "
                f"position {position} starts at {start!r} and ends at {end!r}"
            )
        if start > end:
            raise ValueError(
                "characters instants must not be inverted: "
                f"position {position} starts at {start!r} and ends at {end!r}"
            )
    for position in range(1, len(text)):
        if starts[position] < starts[position - 1] or ends[position] < ends[position - 1]:
            raise ValueError(
                "characters instants must not run backwards: "
                f"position {position} is earlier than {position - 1}"
            )
    return {"text": text, "starts": list(starts), "ends": list(ends)}


def write_archive(archive_root: str | os.PathLike[str], entry: dict[str, Any], segments: list[dict[str, Any]], *, source: str, raw: Any | None = None, asr_provenance: Mapping[str, str] | None = None, characters: Any | None = None, coverage: Mapping[str, Any] | None = None, before_replace: Callable[[], None] | None = None, publication_guard: Callable[[Callable[[], None]], ContextManager[Any]] | None = None) -> dict[str, str]:
    "Publish one transcript bundle below the archive root.\n\n    ``asr_provenance`` carries the ASR runner's redaction-safe configuration\n    (model, revision, device, language, VAD, hotwords).  It is recorded in the\n    raw sidecar and as ``asr_*`` frontmatter keys, so any transcript can be\n    traced back to the model that produced it.  The subtitle path passes\n    nothing and is unchanged.\n\n    ``coverage`` carries this run's coverage attestation (``decoded_s`` /\n    ``produced_s`` / ``coverage`` / ``coverage_min`` / ``coverage_short``, built\n    by :func:`bili_asr.asr.coverage._coverage_record`).  It is recorded as ``coverage_*``\n    frontmatter keys **and** in the raw sidecar, because ``I-000188``'s\n    acceptance names both surfaces: a reader holding only the published bundle\n    must be able to see that the transcript covers part of what was decoded,\n    without reading the store.  The subtitle path has no measurement to carry\n    and passes nothing.\n    "
    try:
        root = _lexical_archive_root(archive_root)
    except OSError as exc:
        raise OSError("archive publication path is unsafe") from exc
    bvid = str(entry["bvid"]) if entry.get("platform", "bilibili") == "bilibili" else None
    finals = bundle_paths(root, entry)
    frontmatter = {"bvid": bvid, "title": entry.get("title", ""), "video_title": entry.get("video_title", ""), "date": entry.get("pubdate_str", ""), "duration_s": entry.get("duration_s", 0), "source": source, "url": archive_url(entry)}
    for field in ("video_description", "uploader_mid", "uploader_name", "video_tags",
                  "video_tags_state", "video_details_observed_at"):
        if field in entry:
            frontmatter[field] = entry[field]
    if entry.get("platform", "bilibili") != "bilibili":
        frontmatter.pop("bvid")
        frontmatter.update(platform=entry["platform"], external_video_id=entry["external_video_id"])
    source_metadata = entry.get("sourceMetadata")
    if source_metadata is not None:
        from bili_asr.source_metadata import SourceMetadataSnapshot

        source_metadata = SourceMetadataSnapshot.from_dict(source_metadata).to_dict()
        frontmatter.update(pubdateUnix=entry["pubdateUnix"], sourcePublishedAt=entry["sourcePublishedAt"],
                           sourceMetadata=source_metadata)
    if source == "asr":
        frontmatter.update(_capture_summary(segments, frontmatter["duration_s"]))
    frontmatter.update(_confidence_summary(segments))
    if asr_provenance:
        frontmatter.update({f"asr_{key}": value for key, value in asr_provenance.items()})
    if coverage:
        # The published span the model produced against the span it decoded.  Both surfaces carry
        # it (I-000188 acceptance: "visible in the store and in the bundle"), and the frontmatter
        # keys are prefixed so a reader can tell the attestation from `asr_*` provenance.
        frontmatter.update({f"coverage_{key}": value for key, value in coverage.items()})
    if entry.get("work_id") and not entry.get("unresolved"):
        frontmatter.update({"work_id": entry["work_id"], "page_index": entry.get("page_index"), "cid": entry.get("cid")})
        if entry.get("platform", "bilibili") != "bilibili":
            frontmatter.pop("cid")
    md = ("---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in frontmatter.items()) + "---\n\n" + segments_to_txt(segments) + "\n").encode("utf-8")
    if raw is None:
        raw = {"segments": segments, "source": source}
        if asr_provenance:
            raw["provenance"] = dict(asr_provenance)
        if coverage:
            raw["coverage"] = dict(coverage)
    if source_metadata is not None and isinstance(raw, dict):
        # v3 has one authoritative source snapshot. A caller-provided raw
        # document must not reintroduce the legacy duplicate field.
        raw.pop("sourceMetadata", None)
        raw.pop("pubdateUnix", None)
        raw.pop("sourcePublishedAt", None)
    # The character-level record rides only on the ASR path: a subtitle-derived raw has no
    # character timings, and inventing them would be a claim about audio nobody aligned.
    block = characters_for(segments, characters) if source == "asr" else {}
    if block:
        raw["characters"] = block
        raw["schema"] = "archive-raw-v2"
    contents = {"srt_path": segments_to_srt(segments).encode(), "vtt_path": segments_to_vtt(segments).encode(), "txt_path": (segments_to_txt(segments) + "\n").encode(), "md_path": md, "raw_path": (json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n").encode()}
    if source_metadata is not None:
        metadata_path = _declared_v3_metadata_path({key: path.relative_to(root).as_posix() for key, path in finals.items()})
        metadata_bytes = (json.dumps(source_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        finals = {**finals, _SOURCE_METADATA_KEY: root / metadata_path}
        contents[_SOURCE_METADATA_KEY] = metadata_bytes
    _publish_bundle(root, finals, contents, before_replace=before_replace, publication_guard=publication_guard)
    return {
        key: os.path.relpath(finals[key], root).replace(os.sep, "/")
        for key in _REQUIRED_ARTIFACT_KEYS
    }
