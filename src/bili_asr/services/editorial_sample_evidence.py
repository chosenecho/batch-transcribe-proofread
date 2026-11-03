"""Offline evidence for the standalone single-product proofreading probe.

This format describes the probe's own ledger, not a production workflow job.
Missing pre-call admission evidence cannot be repaired by a later check.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from bili_asr.canonical_json import digest
from bili_asr.contracts.json_schema import validate_json
from bili_asr.deepseek import normalize_usage, parse_response, request_body
from bili_asr.editorial import (
    TEMPLATE_VERSION,
    EditorialConfig,
    render_documents,
    validate_revision,
)
from bili_asr.editorial_issue_review import annotate_issues
from bili_asr.editorial_lineage import source_lineage
from bili_asr.editorial_quality import check_source_quality
from bili_asr.services.editorial_evaluation import _validate_frozen_chunk
from bili_asr.services.editorial_sample_quality import PRODUCER_FILE, producer_quality
from bili_asr.workflow_errors import JobExecutionError

SCHEMA = "editorial-sample-evidence/v1"
FILES = {"source-private.json": "source_metadata_and_transcripts",
         "prepared-private.json": "frozen_input", "validated-revision-private.json": "validated_revision",
         "calls-private.db": "standalone_call_ledger", "ai-draft.md": "ai_draft", "review.md": "model_review"}


def _read(path: Path):
    return json.loads(path.read_bytes())


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_binding(prepared: dict, source: dict) -> None:
    snapshot = prepared["snapshot"]
    for name in ("base", "reference"):
        frozen, original = snapshot[name], source[name]
        if frozen is None or original is None:
            if frozen != original:
                raise ValueError("source transcript presence mismatch")
            continue
        for key in ("transcript_id", "content_sha256", "source_kind", "language"):
            if frozen[key] != original[key]:
                raise ValueError("source transcript metadata mismatch")
        segments = [dict(s, segment_id=f"t{original['transcript_id']}:s{i}")
                    for i, s in enumerate(original["segments"])]
        if frozen["segments"] != segments or original["video_part_id"] != snapshot["video_part_id"]:
            raise ValueError("source transcript content mismatch")
    metadata = snapshot["metadata"]
    if (source["part"]["video_part_id"] != snapshot["video_part_id"]
            or source["part"]["bvid"] != source["video"]["bvid"]
            or any(metadata[k] != source["video"][k] for k in ("bvid", "title"))
            or metadata["page_index"] != source["part"]["page_index"]):
        raise ValueError("source video metadata mismatch")


def analyze_sample(folder: Path, *, annotations: list[dict] | None = None) -> tuple[dict, dict, dict, dict]:
    """Verify source/request/response bindings without a model or writable DB."""
    prepared, revision = _read(folder / "prepared-private.json"), _read(folder / "validated-revision-private.json")
    blocks, revision_id = revision["blocks"], revision["revision_id"]
    lineage = source_lineage(prepared, blocks, revision_id=revision_id)
    source = _read(folder / "source-private.json")
    _source_binding(prepared, source)
    # The probe was explicitly single-product/single-chunk. Do not guess a
    # chunk association for an imported multi-chunk ledger.
    if len(prepared["chunks"]) != 1:
        raise ValueError("standalone sample requires exactly one chunk")
    chunk = prepared["chunks"][0]
    _validate_frozen_chunk(prepared, chunk)
    config = EditorialConfig.from_snapshot(prepared["snapshot"]["config"])
    original = request_body(chunk, config, system_prompt=prepared["snapshot"]["system_prompt"])
    roles = dict(FILES)
    if (folder / PRODUCER_FILE).exists():
        roles[PRODUCER_FILE] = "producer_quality_evidence"
    calls, validated = [], []
    with closing(sqlite3.connect((folder / "calls-private.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM calls ORDER BY ordinal").fetchall()
    if not rows or [r["ordinal"] for r in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("call ledger ordinals must be contiguous")
    for row in rows:
        prefix = f"call-{row['ordinal']}"
        request_name = prefix + "-request-private.json"
        request = _read(folder / request_name)
        if request != json.loads(row["request_json"]):
            raise ValueError("request differs from call ledger")
        if dict(request, messages=request["messages"][:2]) != original:
            raise ValueError("request differs from frozen provider controls")
        roles[request_name] = "provider_request"
        item = {key: row[key] for key in ("call_id", "ordinal", "outcome", "started_at", "finished_at")}
        item.update(input_id=prepared["input_id"], chunk_id=chunk["chunk_id"],
                    request_fingerprint=digest({"input_id": prepared["input_id"],
                                               "chunk_id": chunk["chunk_id"], "request": request}),
                    linkage_basis="reconstructed_from_verified_frozen_request",
                    invocation_kind="unknown", recovery_metadata_status="not_recorded",
                    feedback_message_count=max(0, len(request["messages"]) - 2),
                    error=None if row["error_json"] is None else json.loads(row["error_json"]))
        if row["response_json"] is not None:
            envelope_name, raw_name = prefix + "-envelope-private.json", prefix + "-response-private.bin"
            envelope = _read(folder / envelope_name)
            if envelope != json.loads(row["response_json"]) or envelope != _read(folder / raw_name):
                raise ValueError("response differs from preserved envelope or ledger")
            roles.update({envelope_name: "provider_envelope", raw_name: "provider_response_bytes"})
            item["usage"] = normalize_usage(envelope)
            if row["outcome"] == "validated":
                validated = validate_revision(chunk, parse_response(envelope))
        elif row["outcome"] == "validated":
            raise ValueError("validated call has no response")
        else:
            # HTTP/protocol failures can leave bytes before the adapter returns
            # an envelope. Preserve that evidence without attaching usage or
            # pretending it was committed to the ledger.
            for suffix, role in (("-envelope-private.json", "provider_envelope"),
                                 ("-response-private.bin", "provider_response_bytes")):
                if (folder / (prefix + suffix)).exists():
                    roles[prefix + suffix] = role
        transport_name = prefix + "-transport.json"
        if (folder / transport_name).exists():
            transport = _read(folder / transport_name)
            roles[transport_name] = "transport_observation"
            item["transport"] = transport
        calls.append(item)
    if validated != blocks:
        raise ValueError("validated model response differs from saved revision")
    if {p.name for p in folder.glob("call-*") if p.is_file()} != {
            name for name in roles if name.startswith("call-")}:
        raise ValueError("sample call artifacts differ from ledger reservations")
    docs = render_documents(prepared["snapshot"]["metadata"], prepared, blocks, revision_id)
    for name, content in docs.items():
        # write_text on Windows translates newlines; preserve these existing
        # bytes but compare text using universal-newline reading.
        if (folder / name).read_text(encoding="utf-8") != content:
            raise ValueError("rendered manuscript differs from its saved revision")
    try:
        check_source_quality(prepared)
        structural = {"status": "passed", "scope": "frozen_cue_structure_and_repetition_only"}
    except JobExecutionError as error:
        structural = {"status": "failed", "error_code": error.error_code, "details": error.safe_details}
    review = annotate_issues(prepared, blocks, revision_id=revision_id,
                             annotations=[] if annotations is None else annotations)
    evidence = {"schema": SCHEMA, "created_at": datetime.now(UTC).isoformat(),
        "input_id": prepared["input_id"], "revision_id": revision_id,
        "template_version": TEMPLATE_VERSION, "quality_status": "needs-review",
        "semantic_status": "unverified", "review_required": True,
        "workflow": {"job_id": None, "attempt_id": None, "reason": "standalone_probe_no_workflow_job"},
        "source_quality": {"pre_call_gate": "not_recorded", "posthoc_structural_check": structural,
                           **producer_quality(folder, prepared)},
        "source_metadata": source["video"] | {"part": source["part"],
            "transcripts": {k: None if source[k] is None else {f: v for f, v in source[k].items() if f != "segments"}
                            for k in ("base", "reference")}},
        "calls": calls}
    return evidence, lineage, review, roles


def package_sample(folder: Path, output: Path, *, annotations: list[dict] | None = None) -> dict:
    """Create a self-contained private package, retaining every original byte."""
    evidence, lineage, review, roles = analyze_sample(folder, annotations=annotations)
    if output.exists():
        raise FileExistsError("sample evidence output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".editorial-evidence-", dir=output.parent) as temporary:
        staging = Path(temporary) / "package"
        originals = staging / "originals"
        originals.mkdir(parents=True)
        artifacts = []
        for name, role in roles.items():
            source = folder / name
            if source.is_symlink() or not source.is_file():
                raise ValueError("sample artifacts must be regular files")
            target = originals / name
            shutil.copyfile(source, target)
            artifacts.append(_artifact(staging, target, role))
        for name, value, role in (("source-lineage.json", lineage, "source_lineage"),
                                  ("issue-review.json", review, "issue_review")):
            _write(staging / name, value)
            artifacts.append(_artifact(staging, staging / name, role))
        evidence["artifacts"] = artifacts
        _write(staging / "manifest.json", evidence)
        verify_package(staging)
        staging.rename(output)
    return evidence


def _artifact(root: Path, path: Path, role: str) -> dict:
    content = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "role": role,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def verify_package(folder: Path) -> dict:
    """Check all declared hashes and replay frozen bindings, without networking."""
    manifest = _read(folder / "manifest.json")
    validate_json(SCHEMA, manifest)
    if (folder / "originals").is_symlink():
        raise ValueError("sample originals must be a regular directory")
    paths = set()
    for artifact in manifest["artifacts"]:
        path = folder / artifact["path"]
        if (path.is_symlink() or not path.is_file() or artifact["path"] != path.relative_to(folder).as_posix()
                or ".." in Path(artifact["path"]).parts or path.resolve().parent not in
                (folder.resolve(), (folder / "originals").resolve()) or artifact["path"] in paths):
            raise ValueError("invalid sample artifact path")
        paths.add(artifact["path"])
        if _artifact(folder, path, artifact["role"]) != artifact:
            raise ValueError("sample artifact checksum mismatch")
    expected, lineage, _, roles = analyze_sample(folder / "originals")
    required = {f"originals/{name}" for name in roles} | {"source-lineage.json", "issue-review.json"}
    if paths != required:
        raise ValueError("sample artifact set mismatch")
    if {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()} != paths | {"manifest.json"}:
        raise ValueError("sample contains unlisted artifacts")
    for key in ("input_id", "revision_id", "template_version", "quality_status", "semantic_status",
                "review_required", "workflow", "source_quality", "source_metadata", "calls"):
        if manifest[key] != expected[key]:
            raise ValueError("sample manifest differs from verified evidence")
    if _read(folder / "source-lineage.json") != lineage:
        raise ValueError("sample lineage differs from frozen sources")
    saved_review = _read(folder / "issue-review.json")
    annotations = []
    for issue in saved_review["issues"]:
        if issue["annotation"] is not None:
            annotations.append({k: issue[k] for k in ("issue_id", "issue_type", "severity", "status", "source_spans")}
                               | issue["annotation"])
    prepared = _read(folder / "originals/prepared-private.json")
    revision = _read(folder / "originals/validated-revision-private.json")
    if saved_review != annotate_issues(prepared, revision["blocks"], revision_id=revision["revision_id"],
                                       annotations=annotations):
        raise ValueError("sample issue review differs from original model issues")
    return manifest
