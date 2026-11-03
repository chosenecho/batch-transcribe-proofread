"""Offline batch input boundary; never resolve a URL or contact a provider."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr.archive_session import ArchiveSession
from bili_asr.artifact_inventory import stream_hash
from bili_asr.artifact_root import roots_for
from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import source_url
from bili_asr.source_video import SourceVideoMetadata
from bili_asr.storage import MetadataRepository, UserRecord, VideoRecord, VideoPartRecord
from bili_asr.storage.sources import SourceRepository
from bili_asr.storage.workflow_outputs import record_audio


def add_input_parser(subparsers, *, archive_root: str) -> None:
    parser = subparsers.add_parser("input", help="Import collected local media without network access")
    actions = parser.add_subparsers(dest="input_action", required=True)
    command = actions.add_parser("import", help="Validate and register a local JSON media manifest")
    command.add_argument("--manifest", required=True, type=Path)
    command.add_argument("--archive-root", default=archive_root)
    command.add_argument("--artifact-root", default=None)


def _probe(path: Path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_type:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    payload = json.loads(result.stdout)
    seconds = float(payload.get("format", {}).get("duration", 0))
    if not payload.get("streams") or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"not a readable audio/video file: {path.name}")
    return max(1, round(seconds * 1000))


def _prepare(manifest: Path) -> list[dict]:
    document = json.loads(manifest.read_text("utf-8-sig"))
    if (not isinstance(document, dict) or set(document) != {"version", "items"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["items"], list) or not 1 <= len(document["items"]) <= 10000):
        raise ValueError("manifest needs version=1 and 1..10000 items")
    prepared, seen = [], set()
    fields = {"platform", "external_id", "page_index", "title", "audio", "sha256",
              "mid", "uploader", "cid", "pubdate", "original_language"}
    for item in document["items"]:
        if not isinstance(item, dict) or set(item) - fields:
            raise ValueError("unsupported manifest item or fields")
        ref = ContentRef(item["platform"], item["external_id"], item.get("page_index", 0))
        source_url(ref)
        if ref in seen:
            raise ValueError("duplicate source identity in manifest")
        seen.add(ref)
        title = item["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be nonempty text")
        path = Path(item["audio"])
        path = path if path.is_absolute() else manifest.parent / path
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("audio must be a local regular file")
        if path.suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".webm", ".mkv"}:
            raise ValueError("unsupported local media extension")
        with path.open("rb") as stream:
            size, digest = stream_hash(stream)
        if item.get("sha256") is not None and item["sha256"] != digest:
            raise ValueError(f"input hash mismatch: {path.name}")
        duration = _probe(path)
        now = int(time.time())
        if ref.platform == "bilibili":
            user = UserRecord(item["mid"], item["uploader"], now, now)
            video = VideoRecord(ref.external_video_id, None, user.mid, title, item["pubdate"], now, now)
            part = VideoPartRecord(ref.external_video_id, ref.part_index, item["cid"], title,
                                   duration, "metadata_collected", now, now)
            records = (user, video, part)
        else:
            records = SourceVideoMetadata(ref, title, duration, original_language=item.get("original_language"),
                                          published_at=item.get("pubdate"), observed_at=now)
        prepared.append(dict(ref=ref, path=path, title=title, size=size, digest=digest,
                             duration=duration, records=records))
    return prepared


def import_manifest(manifest: Path, archive_root: Path, *, artifact_root=None) -> dict:
    """Validate the whole batch, copy verified bytes, then atomically register facts.

    Source identity and publication contracts stay compatible with the collector.
    An existing part's audio cannot be silently replaced underneath frozen jobs.
    """
    prepared = _prepare(manifest.resolve(strict=True))
    roots = roots_for(str(archive_root), flag_value=artifact_root, require_writable=True)
    with ArchiveSession(archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots).open() as session:
        connection = session.connection
        metadata = MetadataRepository(connection)
        with metadata.transaction():
            for item in prepared:
                ref = item["ref"]
                if ref.platform == "bilibili":
                    user, video, part = item["records"]
                    metadata.upsert_user(user)
                    metadata.upsert_video(video)
                    part_id = metadata.upsert_part(part)
                else:
                    part_id = SourceRepository(connection).upsert_video(item["records"])
                previous = connection.execute(
                    "SELECT a.sha256 FROM part_audio_objects p JOIN audio_objects a USING(audio_id) "
                    "WHERE p.video_part_id=?", (part_id,),
                ).fetchall()
                if any(row[0] != item["digest"] for row in previous):
                    raise ValueError(f"part {part_id} already has different audio; preserve its frozen input")
                item["part_id"] = part_id
            directory = roots.write_base / "audio"
            directory.mkdir(parents=True, exist_ok=True)
            # A rollback can leave verified, content-addressed files, but cannot
            # expose a partially registered input or overwrite different bytes.
            for item in prepared:
                key = f"audio/{item['digest']}{item['path'].suffix.lower()}"
                existing = connection.execute("SELECT storage_key FROM audio_objects WHERE sha256=?",
                                              (item["digest"],)).fetchone()
                if existing is not None:
                    key = existing[0]
                from bili_asr.path_policy import confined_audio_path
                target = confined_audio_path(roots.write_base, key, require_exists=False)
                if target is None:
                    raise ValueError("audio destination is unavailable")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    with target.open("rb") as stream:
                        if stream_hash(stream) != (item["size"], item["digest"]):
                            raise ValueError("existing imported audio has changed")
                else:
                    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
                        staged = Path(temporary.name)
                    created = False
                    try:
                        shutil.copyfile(item["path"], staged)
                        with staged.open("rb") as stream:
                            if stream_hash(stream) != (item["size"], item["digest"]):
                                raise ValueError("input changed while copying")
                        # Exclusive creation keeps another importer from being overwritten.
                        with target.open("xb") as output, staged.open("rb") as source:
                            created = True
                            shutil.copyfileobj(source, output)
                            output.flush()
                            os.fsync(output.fileno())
                    except BaseException:
                        # A partial copy has no DB registration; retain a valid
                        # preexisting destination and remove only our own copy.
                        if created and target.exists():
                            with target.open("rb") as stream:
                                complete = stream_hash(stream) == (item["size"], item["digest"])
                            if not complete:
                                target.unlink()
                        raise
                    finally:
                        staged.unlink(missing_ok=True)
                record_audio(connection, part_id=item["part_id"], sha256=item["digest"],
                             byte_size=item["size"], media_format=item["path"].suffix[1:].lower(),
                             duration_ms=item["duration"], storage_key=key,
                             target_id="local-artifacts" if roots.configured else "local")
        return {"imported": len(prepared), "part_ids": [item["part_id"] for item in prepared]}


def _cmd_input(args: argparse.Namespace) -> int:
    from bili_asr.diagnostics import write_stderr
    try:
        print(json.dumps(import_manifest(args.manifest, Path(args.archive_root),
                                        artifact_root=args.artifact_root), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        write_stderr(f"input import: {error}")
        return 1
