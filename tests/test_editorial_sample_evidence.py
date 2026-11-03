"""Private probe packages are portable, byte preserving and offline replayable."""
import hashlib
import json
import shutil
import socket
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from bili_asr.canonical_json import digest
from bili_asr.deepseek import request_body
from bili_asr.editorial import (
    EditorialConfig,
    prepare_input,
    render_documents,
    validate_revision,
)
from bili_asr.editorial_issue_review import issue_review
from bili_asr.services.editorial_sample_evidence import (
    analyze_sample,
    package_sample,
    verify_package,
)
from tests.test_ai_editorial import envelope, no_changes, record


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sample(tmp_path):
    folder = tmp_path / "sample"
    folder.mkdir()
    base = record()
    metadata = {"title": "离线样本", "bvid": "BV1aRTA6mEGF", "page_index": 0}
    prepared = prepare_input(base, None, EditorialConfig(), metadata=metadata)
    chunk = prepared["chunks"][0]
    model_output = no_changes(chunk)
    model_output["paragraphs"][0]["issues"] = [{"note": "核实文字", "candidate": "", "evidence_refs": ["t1:s0"]}]
    response = envelope(model_output)
    response["usage"].update(total_tokens=180, prompt_tokens_details={"cached_tokens": 0})
    request = request_body(chunk, EditorialConfig(), system_prompt=prepared["snapshot"]["system_prompt"])
    blocks = validate_revision(chunk, model_output)
    revision = digest({"input_id": prepared["input_id"], "blocks": blocks})
    write(folder / "prepared-private.json", prepared)
    write(folder / "validated-revision-private.json", {"revision_id": revision, "blocks": blocks})
    write(folder / "source-private.json", {"base": asdict(base), "reference": None,
          "video": {"title": metadata["title"], "bvid": metadata["bvid"], "mid": 23191782,
                    "aid": 123, "pubdate": 1234},
          "part": {"video_part_id": 1, "bvid": metadata["bvid"], "page_index": 0, "cid": 456, "duration_ms": 2000}})
    write(folder / "call-1-request-private.json", request)
    write(folder / "call-1-envelope-private.json", response)
    write(folder / "call-1-response-private.bin", response)
    write(folder / "call-1-transport.json", {"status_code": 200})
    with closing(sqlite3.connect(folder / "calls-private.db")) as db:
        db.execute("CREATE TABLE calls(call_id TEXT PRIMARY KEY, ordinal INTEGER, request_json TEXT, "
                   "response_json TEXT, outcome TEXT, error_json TEXT, started_at TEXT, finished_at TEXT)")
        db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)", (
            "call-1", 1, json.dumps(request), json.dumps(response), "validated", None,
            "2026-10-14T12:00:00+00:00", "2026-10-14T12:00:01+00:00"))
        db.commit()
    for name, text in render_documents(metadata, prepared, blocks, revision).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_package_is_portable_offline_preserves_bytes_and_never_certifies_semantics(sample, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("evidence reconstruction must not use a network")
    monkeypatch.setattr(socket, "create_connection", refuse)
    output = tmp_path / "package"
    result = package_sample(sample, output)
    for artifact in result["artifacts"]:
        if artifact["path"].startswith("originals/"):
            assert (output / artifact["path"]).read_bytes() == (sample / Path(artifact["path"]).name).read_bytes()
    call = result["calls"][0]
    assert call["usage"]["total_tokens"] == 180 and call["usage"]["cached_tokens"] == 0
    assert call["input_id"] == result["input_id"] and call["linkage_basis"].startswith("reconstructed")
    assert call["invocation_kind"] == "unknown" and call["recovery_metadata_status"] == "not_recorded"
    assert result["workflow"]["job_id"] is None and result["workflow"]["attempt_id"] is None
    assert result["quality_status"] == "needs-review" and result["semantic_status"] == "unverified"
    assert result["source_quality"]["producer_admission"] == "unknown"
    assert result["source_quality"]["posthoc_structural_check"]["status"] == "passed"
    assert result["source_metadata"]["mid"] == 23191782
    shutil.rmtree(sample)
    assert verify_package(output) == result
    with pytest.raises(FileExistsError):
        package_sample(output / "originals", output)


@pytest.mark.parametrize("damage", ["source", "request", "response", "revision", "manuscript"])
def test_detached_sources_requests_or_outputs_are_rejected_before_packaging(sample, tmp_path, damage):
    if damage == "source":
        path = sample / "source-private.json"
        value = json.loads(path.read_bytes()); value["base"]["segments"][0]["text"] = "篡改"
    elif damage == "request":
        path = sample / "call-1-request-private.json"
        value = json.loads(path.read_bytes()); value["model"] = "different"
    elif damage == "response":
        path = sample / "call-1-response-private.bin"
        value = json.loads(path.read_bytes()); value["usage"]["total_tokens"] = 999
    elif damage == "revision":
        path = sample / "validated-revision-private.json"
        value = json.loads(path.read_bytes()); value["revision_id"] = "a" * 64
    else:
        (sample / "ai-draft.md").write_text("篡改", encoding="utf-8")
        path = None
    if path: write(path, value)
    with pytest.raises(ValueError): package_sample(sample, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("damage", ["bytes", "manifest", "missing", "extra", "traversal", "lineage"])
def test_package_verification_rejects_corruption_and_false_quality(sample, tmp_path, damage):
    output = tmp_path / "output"
    package_sample(sample, output)
    manifest = json.loads((output / "manifest.json").read_bytes())
    if damage == "bytes": (output / "originals/ai-draft.md").write_bytes(b"tampered")
    elif damage == "manifest": manifest["semantic_status"] = "verified"
    elif damage == "missing": manifest["artifacts"].pop()
    elif damage == "extra": manifest["artifacts"].append(manifest["artifacts"][0])
    elif damage == "traversal": manifest["artifacts"][0]["path"] = "../sample/source-private.json"
    else:
        path = output / "source-lineage.json"
        value = json.loads(path.read_bytes()); value["semantic_status"] = "verified"; write(path, value)
        entry = next(a for a in manifest["artifacts"] if a["path"] == path.name)
        entry.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    write(output / "manifest.json", manifest)
    with pytest.raises(ValueError): verify_package(output)


def test_failed_attempt_is_retained_without_inventing_missing_response(sample):
    with closing(sqlite3.connect(sample / "calls-private.db")) as db:
        row = db.execute("SELECT * FROM calls").fetchone()
        db.execute("UPDATE calls SET ordinal=2")
        db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)", (
            "failed-call", 1, row[2], None, "failed", '{"error_code":"timeout"}', row[6], row[7]))
        db.commit()
    for suffix in ("request-private.json", "envelope-private.json", "response-private.bin", "transport.json"):
        shutil.copyfile(sample / f"call-1-{suffix}", sample / f"call-2-{suffix}")
    # The old first response cannot be attached to a failed ledger reservation.
    for suffix in ("envelope-private.json", "response-private.bin", "transport.json"):
        (sample / f"call-1-{suffix}").unlink()
    evidence, _, _, roles = analyze_sample(sample)
    assert len(evidence["calls"]) == 2
    assert evidence["calls"][0]["error"]["error_code"] == "timeout"
    assert "usage" not in evidence["calls"][0]
    assert "call-1-envelope-private.json" not in roles


def test_failed_http_response_bytes_are_preserved_without_inventing_usage(sample, tmp_path):
    test_failed_attempt_is_retained_without_inventing_missing_response(sample)
    (sample / "call-1-response-private.bin").write_bytes(b'{"error":"payment required"}')
    result = package_sample(sample, tmp_path / "package")
    assert "usage" not in result["calls"][0]
    assert any(a["path"] == "originals/call-1-response-private.bin" for a in result["artifacts"])


def test_separately_attributed_annotations_survive_package_replay(sample, tmp_path):
    prepared = json.loads((sample / "prepared-private.json").read_bytes())
    revision = json.loads((sample / "validated-revision-private.json").read_bytes())
    task = issue_review(prepared, revision["blocks"], revision_id=revision["revision_id"])["issues"][0]
    annotation = {"issue_id": task["issue_id"], "issue_type": "recognition", "severity": "medium", "status": "open",
                  "source_spans": [{"segment_id": "t1:s0", "start_char": 0, "end_char": 2, "quote": "这是"}],
                  "reviewer_kind": "agent", "reviewer": "test-agent", "reviewed_at": "2026-10-14T12:00:00+00:00",
                  "note": "文字定位，未听音频。"}
    output = tmp_path / "package"
    result = package_sample(sample, output, annotations=[annotation])
    assert verify_package(output) == result
    saved = json.loads((output / "issue-review.json").read_bytes())["issues"][0]
    assert saved["span_status"] == "located" and saved["model_issue"] == task["model_issue"]


def test_unlinked_paid_call_artifacts_are_rejected(sample):
    (sample / "call-2-response-private.bin").write_bytes(b"unlinked response")
    with pytest.raises(ValueError, match="ledger reservations"):
        analyze_sample(sample)


def producer_export(sample):
    base = json.loads((sample / "prepared-private.json").read_bytes())["snapshot"]["base"]
    return {"observed_at": "2026-10-14T12:00:00+00:00", "transcript_id": base["transcript_id"],
            "content_sha256": base["content_sha256"], "rows": {
                "transcript_asr_evidence": [{"run_id": "producer-run", "video_part_id": 1,
                    "transcript_id": base["transcript_id"], "evidence_json": json.dumps({
                        "schema_version": 1, "audio": {"duration_ms": 1900}})}],
                "workflow_quality_assessments": [], "transcript_coverage_attestations": [{
                    "run_id": "producer-run", "transcript_id": base["transcript_id"], "video_part_id": 1,
                    "decoded_s": 1.9, "produced_s": 1.9, "coverage": 1.0,
                    "coverage_min": 0.97, "coverage_short": 0}]}}


def test_measured_producer_admission_is_posthoc_and_does_not_certify_ai_output(sample, tmp_path):
    write(sample / "producer-quality-posthoc.json", producer_export(sample))
    result = package_sample(sample, tmp_path / "output")
    quality = result["source_quality"]
    assert quality["producer_admission"] == "usable"
    assert quality["producer_check"]["run_id"] == "producer-run"
    assert quality["producer_check"]["basis"] == "posthoc_exported_producer_evidence"
    assert quality["pre_call_gate"] == "not_recorded" and result["semantic_status"] == "unverified"
    assert any(a["role"] == "producer_quality_evidence" for a in result["artifacts"])


@pytest.mark.parametrize("damage", ["different-transcript", "different-source", "wrong-run", "coverage-missing",
                                    "multiple-producers", "old-policy", "backward-cue", "wrong-part",
                                    "unsupported-schema", "embedded-identity", "coverage-part", "invalid-coverage"])
def test_missing_ambiguous_or_bad_producer_evidence_never_becomes_usable(sample, damage):
    export = producer_export(sample)
    expected = "unknown"
    if damage == "different-transcript": export["transcript_id"] = 999
    elif damage == "different-source": export["content_sha256"] = "a" * 64
    elif damage == "wrong-run": export["rows"]["transcript_coverage_attestations"][0]["run_id"] = "foreign"
    elif damage == "coverage-missing": export["rows"]["transcript_coverage_attestations"] = []
    elif damage == "multiple-producers": export["rows"]["transcript_asr_evidence"] *= 2
    elif damage == "old-policy": export["rows"]["workflow_quality_assessments"] = [{"status": "unusable"}]
    elif damage == "wrong-part": export["rows"]["transcript_asr_evidence"][0]["video_part_id"] = 999
    elif damage == "unsupported-schema":
        export["rows"]["transcript_asr_evidence"][0]["evidence_json"] = json.dumps({"schema_version": 2})
    elif damage == "embedded-identity":
        export["rows"]["transcript_asr_evidence"][0]["evidence_json"] = json.dumps({
            "schema_version": 1, "run_id": "foreign"})
    elif damage == "coverage-part": export["rows"]["transcript_coverage_attestations"][0]["video_part_id"] = 999
    elif damage == "invalid-coverage": export["rows"]["transcript_coverage_attestations"][0]["coverage_short"] = True
    else:
        export["rows"]["transcript_coverage_attestations"][0]["produced_s"] = 0.5
        expected = "unusable"
    write(sample / "producer-quality-posthoc.json", export)
    if damage in ("different-transcript", "different-source", "wrong-part", "embedded-identity",
                  "coverage-part", "invalid-coverage"):
        with pytest.raises(ValueError): analyze_sample(sample)
    elif damage == "unsupported-schema":
        result = analyze_sample(sample)[0]
        assert result["source_quality"]["producer_admission"] == expected
    else:
        result = analyze_sample(sample)[0]
        assert result["source_quality"]["producer_admission"] == expected


def test_producer_run_identity_mismatch_is_rejected(sample):
    export = producer_export(sample)
    export["rows"]["transcript_asr_evidence"][0]["video_part_id"] = 999
    write(sample / "producer-quality-posthoc.json", export)
    with pytest.raises(ValueError, match="producer run transcript mismatch"):
        analyze_sample(sample)


def test_unsupported_producer_evidence_remains_unknown(sample):
    export = producer_export(sample)
    evidence = json.loads(export["rows"]["transcript_asr_evidence"][0]["evidence_json"])
    evidence["schema_version"] = 2
    export["rows"]["transcript_asr_evidence"][0]["evidence_json"] = json.dumps(evidence)
    write(sample / "producer-quality-posthoc.json", export)
    result = analyze_sample(sample)[0]
    assert result["source_quality"]["producer_admission"] == "unknown"
    assert result["source_quality"]["producer_check"]["basis"] == "posthoc_exported_producer_evidence"
    assert result["source_quality"]["reason"] == "unsupported_producer_evidence"


def test_embedded_producer_identity_mismatch_is_rejected(sample):
    export = producer_export(sample)
    evidence = json.loads(export["rows"]["transcript_asr_evidence"][0]["evidence_json"])
    evidence["run_id"] = "different-run"
    export["rows"]["transcript_asr_evidence"][0]["evidence_json"] = json.dumps(evidence)
    write(sample / "producer-quality-posthoc.json", export)
    with pytest.raises(ValueError, match="embedded producer evidence identity mismatch"):
        analyze_sample(sample)


def test_producer_coverage_part_mismatch_is_rejected(sample):
    export = producer_export(sample)
    export["rows"]["transcript_coverage_attestations"][0]["video_part_id"] = 999
    write(sample / "producer-quality-posthoc.json", export)
    with pytest.raises(ValueError, match="producer coverage part mismatch"):
        analyze_sample(sample)


@pytest.mark.parametrize("coverage_short", [2, True, "0"])
def test_invalid_producer_coverage_flag_is_rejected(sample, coverage_short):
    export = producer_export(sample)
    export["rows"]["transcript_coverage_attestations"][0]["coverage_short"] = coverage_short
    write(sample / "producer-quality-posthoc.json", export)
    with pytest.raises(ValueError, match="invalid exported producer coverage flag"):
        analyze_sample(sample)
