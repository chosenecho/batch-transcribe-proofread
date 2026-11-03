"""A production quality boundary, independent of manuscript review and releases.

The receipt describes the bytes at handoff. It never monitors or imports later
edits made in a data repository. Rich metadata and extension fields stay intact.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from bili_asr import __version__
from bili_asr.asr.admission import assess_output
from bili_asr.canonical_json import digest
from bili_asr.editorial_lineage import source_lineage
from bili_asr.editorial_quality import check_source_quality
from bili_asr.editorial_segments import segments
from bili_asr.export_snapshot import guard_output
from bili_asr.manuscript_artifacts import get_ai_artifacts
from bili_asr.portable_bundle import (
    copy_file,
    json_bytes,
    new_bundle,
    seal_bundle,
    verify_bundle,
    write_file,
)
from bili_asr.storage.editorial import EditorialRepository

FORMAT = "asr-delivery/v1"
GATE = "production-minimum/v1"


@contextmanager
def _read_transaction(connection):
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            connection.rollback()


def _source(metadata: dict) -> dict:
    frozen = metadata.get("source_metadata")
    if frozen is not None:
        from bili_asr.source_metadata import SourceMetadataSnapshot
        ref = SourceMetadataSnapshot.from_dict(frozen).ref
        platform, external_id, part = ref.platform, ref.external_video_id, ref.part_index
    else:
        platform, external_id, part = "bilibili", metadata["bvid"], metadata["page_index"]
    return {"platform": platform, "source_id": external_id, "part_index": part,
            "group_id": f"{platform}:{external_id}"}


def validate_record(record: dict) -> None:
    """Enforce only portable identity, usable text, timeline and quality status.

    Additional top-level and nested fields are intentionally not enumerated.
    Training approval, speakers, answers and retrieval metadata belong downstream.
    """
    if not isinstance(record, dict):
        raise ValueError("delivery records must be objects")  # noqa: TRY004 - a wire contract failure
    for key in ("record_id", "kind", "text"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError("delivery record requires nonempty " + key)
    source = record.get("source")
    if not isinstance(source, dict) or any(not isinstance(source.get(key), str) or not source[key].strip()
                                           for key in ("platform", "source_id", "group_id")):
        raise ValueError("delivery record requires a stable source identity")
    quality = record.get("quality")
    if not isinstance(quality, dict) or quality.get("status") != "passed" or quality.get("gate") != GATE:
        raise ValueError("delivery record has not passed the production quality gate")
    if not isinstance(record.get("provenance"), dict) or not record["provenance"]:
        raise ValueError("delivery record requires provenance")
    timeline = record.get("segments")
    if timeline is not None:
        if not isinstance(timeline, list) or not timeline:
            raise ValueError("delivery timeline must be a nonempty list when present")
        previous = -1
        for segment in timeline:
            if (not isinstance(segment, dict) or type(segment.get("start_ms")) is not int
                    or type(segment.get("end_ms")) is not int
                    or segment["start_ms"] < 0 or segment["end_ms"] <= segment["start_ms"]
                    or segment["start_ms"] < previous):
                raise ValueError("delivery timeline is invalid; do not sort or clamp evidence")
            previous = segment["start_ms"]
    # Refuse NaN, unsupported Python values, etc., without pruning extra fields.
    json_bytes(record)


def read_records(path: Path) -> list[dict]:
    result, ids = [], set()
    with path.open(encoding="utf-8") as stream:
        for ordinal, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"empty delivery record at line {ordinal}")
            record = json.loads(line)
            validate_record(record)
            if record["record_id"] in ids:
                raise ValueError("duplicate delivery record identity")
            ids.add(record["record_id"])
            result.append(record)
    if not result:
        raise ValueError("delivery contains no records")
    return result


def verify_delivery(root: Path) -> dict:
    manifest = verify_bundle(root, format=FORMAT)
    if manifest.get("records_file") != "records.jsonl":
        raise ValueError("unsupported delivery records path")
    records = read_records(Path(root) / "records.jsonl")
    if type(manifest.get("record_count")) is not int or manifest["record_count"] != len(records):
        raise ValueError("delivery record count differs from receipt")
    return manifest


def _quality(cues: list[dict], *, warning: str | None = None) -> dict:
    assessed = assess_output([{"start": s["start_ms"] / 1000, "end": s["end_ms"] / 1000,
                               "text": s["text"]} for s in cues])
    if assessed["status"] == "unusable":
        raise ValueError("source transcript fails production timeline/text checks")
    return {"gate": GATE, "status": "passed", "semantic_status": "unverified",
            "human_review_status": "not_reviewed", "source_assessment": assessed,
            "warnings": [] if warning is None else [warning]}


def export_delivery(connection, *, output: Path, artifact_roots: tuple[Path, ...],
                    revision_ids=(), transcript_ids=(), extensions: dict | None = None,
                    attachments: dict[str, Path] | None = None) -> dict:
    """Freeze selected outputs; never create editions, approvals or releases."""
    if not revision_ids and not transcript_ids:
        raise ValueError("select at least one explicit revision or transcript")
    if extensions is not None and not isinstance(extensions, dict):
        raise ValueError("delivery extensions must be an object")
    output = guard_output(connection, Path(output), artifact_roots)
    records, files = [], {}
    repository = EditorialRepository(connection)

    def raw_transcript(transcript_id):
        source = repository.read_source(transcript_id)
        name = f"bronze/transcript-{transcript_id}.json"
        files[name] = json_bytes(asdict(source))
        model = (connection.execute("SELECT model_name,revision FROM asr_models WHERE model_id=?",
                                    (source.model_id,)).fetchone() if source.model_id is not None else None)
        files[f"bronze/transcript-{transcript_id}-model.json"] = json_bytes(None if model is None else dict(model))
        evidence = connection.execute("SELECT * FROM transcript_asr_evidence WHERE transcript_id=? ORDER BY rowid",
                                      (transcript_id,)).fetchall()
        files[f"bronze/transcript-{transcript_id}-asr-evidence.jsonl"] = b"".join(
            json_bytes(dict(row)) for row in evidence)
        return source, name

    with _read_transaction(connection):
        for revision_id in dict.fromkeys(revision_ids):
            prepared, blocks = repository.revision(revision_id)
            base = prepared["snapshot"]["base"]
            source, raw_name = raw_transcript(base["transcript_id"])
            if source.content_sha256 != base["content_sha256"] or segments(source) != base["segments"]:
                raise ValueError("frozen editorial source differs from its transcript evidence")
            check_source_quality(prepared, connection=connection)
            lineage = source_lineage(prepared, blocks, revision_id=revision_id)
            pair = get_ai_artifacts(connection, revision_id, artifact_roots)
            row = connection.execute("SELECT * FROM editorial_revisions WHERE revision_id=?", (revision_id,)).fetchone()
            stored_input = connection.execute("SELECT prepared_json FROM editorial_inputs WHERE input_id=?",
                                              (prepared["input_id"],)).fetchone()[0]
            prefix = f"bronze/editorial-{revision_id}/"
            files.update({prefix + name: content for name, content in pair.items()})
            files[prefix + "input.json"] = stored_input.encode("utf-8")
            files[prefix + "revision.json"] = json_bytes(dict(row))
            files[prefix + "lineage.json"] = json_bytes(lineage)
            calls = connection.execute("SELECT * FROM editorial_model_calls WHERE input_id=? ORDER BY rowid",
                                       (prepared["input_id"],)).fetchall()
            # request_json/response_json stay exact strings, including unknown provider fields.
            files[prefix + "model-calls.jsonl"] = b"".join(json_bytes(dict(call)) for call in calls)
            if prepared["snapshot"]["reference"] is not None:
                raw_transcript(prepared["snapshot"]["reference"]["transcript_id"])
            metadata = prepared["snapshot"]["metadata"]
            records.append({"record_id": "editorial:" + revision_id, "kind": "editorial",
                "source": _source(metadata), "text": "\n\n".join(block["text"] for block in blocks),
                "segments": blocks, "metadata": metadata,
                "quality": _quality(base["segments"], warning=row["quality_status"]),
                "provenance": {"revision_id": revision_id, "input_id": prepared["input_id"],
                    "transcript_id": source.transcript_id, "transcript_file": raw_name,
                    "draft_file": prefix + "ai-draft.md", "review_file": prefix + "review.md",
                    "input_file": prefix + "input.json", "revision_file": prefix + "revision.json",
                    "model_calls_file": prefix + "model-calls.jsonl",
                    "lineage_file": prefix + "lineage.json"},
                "extensions": extensions or {}})
        for transcript_id in dict.fromkeys(transcript_ids):
            source, raw_name = raw_transcript(transcript_id)
            cues = segments(source)
            metadata = repository.metadata(source.video_part_id)
            quality = _quality(cues)
            if source.source_kind == "asr-local":
                from bili_asr.storage.asr_quality import (
                    assess_transcript,
                    require_admission,
                )
                admission = assess_transcript(connection, source.transcript_id)
                require_admission(admission, stage="delivery")
                quality["admission"] = admission
            model = (connection.execute("SELECT model_name,revision FROM asr_models WHERE model_id=?",
                                        (source.model_id,)).fetchone() if source.model_id is not None else None)
            identity = digest({"source": _source(metadata), "content_sha256": source.content_sha256,
                               "source_kind": source.source_kind, "model": None if model is None else dict(model),
                               "language": source.language, "version": source.version})
            records.append({"record_id": "transcript:" + identity,
                "kind": "transcript", "source": _source(metadata),
                "text": "\n".join(item["text"] for item in cues), "segments": cues,
                "metadata": metadata, "quality": quality,
                "provenance": {"transcript_id": source.transcript_id, "transcript_file": raw_name},
                "extensions": extensions or {}})
    for name, path in (attachments or {}).items():
        # Caller-selected user/profile data is retained verbatim as an attachment.
        # It is not falsely described as a validated transcript or approved sample.
        if Path(path).is_symlink() or not Path(path).is_file():
            raise ValueError("delivery attachment must be a regular file")
        files["attachments/" + name] = Path(path)
    for record in records:
        validate_record(record)
    files["records.jsonl"] = b"".join(json_bytes(record) for record in records)
    with new_bundle(output) as stage:
        for name, content in files.items():
            if isinstance(content, Path):
                copy_file(stage, name, content)
            else:
                write_file(stage, name, content)
        manifest = seal_bundle(stage, format=FORMAT, files=list(files), producer_version=__version__,
            records_file="records.jsonl", record_count=len(records), quality_gate=GATE,
            lifecycle="producer-responsibility-ends-at-handoff")
        verify_delivery(stage)
    return {"output": str(output), "bundle_id": manifest["bundle_id"], "record_count": len(records)}
