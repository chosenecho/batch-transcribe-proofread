"""WebVTT formatting, complete bundles and publication visibility contracts."""

from contextlib import contextmanager
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path

import pytest

from bili_asr import archive
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.artifacts import BUNDLE_SCHEMA, REQUIRED_ARTIFACT_KEYS
from bili_asr.cli.main import main
from bili_asr.coverage_report import CoverageReport
from bili_asr.cues import segments_to_srt, segments_to_vtt
from bili_asr.export import export_records, format_csv_export
from bili_asr.integrity import IntegrityVerifier
from bili_asr.search_index.readers import extract_transcript_text
from bili_asr.services.bundle_verification import verify_bundle
from bili_asr.services.transcript_adoption import read_archived_transcript
from bili_asr.services.workflow_projection import workflow_records
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import SourceMetadataSnapshot
from bili_asr.storage import (
    AcquisitionRunRecord, TranscriptRepository, TranscriptSegmentRecord,
    WorkflowRepository, open_database,
)
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers


ENTRY = {
    "bvid": "BVvtt", "work_id": "BVvtt:p0", "page_index": 0, "cid": 1,
    "title": "中文测试", "video_title": "归档视频", "duration_s": 8,
    "pubdate_str": "2026-10-08",
}


def test_v3_bundle_freezes_metadata_in_a_hashed_sidecar(tmp_path):
    entry = {
        **ENTRY,
        "pubdateUnix": 1_700_000_000,
        "sourcePublishedAt": "2023-11-14T22:13:20Z",
        "sourceMetadata": SourceMetadataSnapshot(
            ContentRef("bilibili", ENTRY["bvid"], 0), "归档视频", "42", "作者", 1_700_000_000, 1_700_000_100,
            description="source description", tags=("tag",),
        ).to_dict(),
    }
    paths = archive.write_archive(tmp_path, entry, [{"start": 0, "end": 1, "text": "text"}], source="asr")
    bundle = tmp_path / paths["srt_path"].replace("bundle.srt", "")
    marker = json.loads((bundle / ".bundle-ready").read_text(encoding="ascii"))
    raw = json.loads((bundle / "bundle.raw.json").read_text(encoding="utf-8"))
    assert marker["schema"] == "archive-bundle-v3"
    assert set(marker["artifacts"]) == set(REQUIRED_ARTIFACT_KEYS) | {"source_metadata_path"}
    assert "sourceMetadata" not in raw
    assert archive.archive_bundle_complete(tmp_path, paths)
    (bundle / "source-metadata.json").write_text("{}\n", encoding="utf-8")
    assert not archive.archive_bundle_complete(tmp_path, paths)
SEGMENTS = [
    {"start": 0, "end": 2.501, "text": "第一行\n第二行 <b> & 字面 &amp;"},
    {"start": 2, "end": 4, "text": "允许重叠的下一句"},
]


def _bundle(root, *, segments=SEGMENTS, **kwargs):
    return archive.write_archive(root, ENTRY, segments, source="subtitle-ai", **kwargs)


def test_empty_document_has_header_and_blank_separator():
    assert segments_to_vtt([]) == "WEBVTT\n\n"


def test_vtt_preserves_order_timing_multiline_and_plain_text():
    document = segments_to_vtt(SEGMENTS)
    assert document == (
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.501\n"
        "第一行\n第二行 &lt;b&gt; &amp; 字面 &amp;amp;\n\n"
        "2\n00:00:02.000 --> 00:00:04.000\n允许重叠的下一句\n\n"
    )
    vtt_blocks = document.removeprefix("WEBVTT\n\n").strip().split("\n\n")
    srt_blocks = segments_to_srt(SEGMENTS).strip().split("\n\n")
    for vtt, srt, segment in zip(vtt_blocks, srt_blocks, SEGMENTS, strict=True):
        vtt_lines, srt_lines = vtt.splitlines(), srt.splitlines()
        assert vtt_lines[1] == srt_lines[1].replace(",", ".")
        assert html.unescape("\n".join(vtt_lines[2:])) == segment["text"]


@pytest.mark.parametrize("start,end,expected", [
    (0.0006, 1.2346, "00:00:00.001 --> 00:00:01.235"),
    (3599.9996, 3601.0004, "01:00:00.000 --> 01:00:01.000"),
    (360000, 360001.002, "100:00:00.000 --> 100:00:01.002"),
])
def test_timestamp_rounding_and_hour_boundaries(start, end, expected):
    assert expected in segments_to_vtt([{"start": start, "end": end, "text": "cue"}])


def test_crlf_and_cr_payload_lines_are_normalized_without_flattening():
    assert "one\ntwo\nthree\n\n" in segments_to_vtt([
        {"start": 0, "end": 1, "text": "one\r\ntwo\rthree"},
    ])


@pytest.mark.parametrize("segment", [
    {"start": -1, "end": 1, "text": "cue"},
    {"start": 0, "end": 0, "text": "cue"},
    {"start": 0, "end": 0.0001, "text": "cue"},
    {"start": float("nan"), "end": 1, "text": "cue"},
    {"start": 0, "end": float("inf"), "text": "cue"},
    {"start": True, "end": 1, "text": "cue"},
    {"start": 0, "end": 1, "text": ""},
    {"start": 0, "end": 1, "text": "one\n\ntwo"},
    {"start": 0, "end": 1, "text": "one\n \ntwo"},
    {"start": 0, "end": 1, "text": "\none"},
    {"start": 0, "end": 1, "text": "one\n"},
    {"start": 0, "end": 1, "text": "one\x00two"},
])
def test_unrepresentable_cue_is_explicitly_refused(segment):
    with pytest.raises(ValueError, match="WebVTT cue 1 is unrepresentable"):
        segments_to_vtt([segment])


def test_decreasing_starts_refuse_entire_publish_without_sorting(tmp_path):
    paths = _bundle(tmp_path)
    before = {path: (tmp_path / path).read_bytes() for path in paths.values()}
    with pytest.raises(ValueError, match="cue starts must not decrease"):
        _bundle(tmp_path, segments=list(reversed(SEGMENTS)))
    assert before == {path: (tmp_path / path).read_bytes() for path in paths.values()}
    assert archive.archive_bundle_complete(tmp_path, paths)


def test_five_products_and_v2_marker_certify_vtt_bytes(tmp_path):
    paths = _bundle(tmp_path)
    assert tuple(paths) == REQUIRED_ARTIFACT_KEYS
    assert paths["vtt_path"] == "transcripts/BVvtt.p0/bundle.vtt"
    assert (tmp_path / paths["vtt_path"]).read_bytes() == segments_to_vtt(SEGMENTS).encode("utf-8")
    marker = archive.bundle_marker_path(tmp_path / paths["srt_path"])
    document = json.loads(marker.read_bytes())
    assert document["schema"] == BUNDLE_SCHEMA == "archive-bundle-v2"
    assert set(document["artifacts"]) == set(REQUIRED_ARTIFACT_KEYS)
    assert document["artifacts"]["vtt_path"] == {
        "path": paths["vtt_path"],
        "sha256": hashlib.sha256((tmp_path / paths["vtt_path"]).read_bytes()).hexdigest(),
    }
    assert archive.archive_bundle_complete(tmp_path, paths)
    assert verify_bundle(tmp_path, paths)
    _text, reader_paths = extract_transcript_text(tmp_path, {**ENTRY, **paths})
    assert "vtt_path" in reader_paths


@pytest.mark.parametrize("damage", ["missing", "same_stat_edit", "marker_entry", "old_schema", "four_paths"])
def test_vtt_is_required_by_direct_and_isolated_verification(tmp_path, damage):
    paths = _bundle(tmp_path)
    target = tmp_path / paths["vtt_path"]
    marker = archive.bundle_marker_path(tmp_path / paths["srt_path"])
    if damage == "missing":
        target.unlink()
    elif damage == "same_stat_edit":
        os.utime(target, (1_700_000_000, 1_700_000_000))
        before = target.stat()
        payload = target.read_bytes()
        target.write_bytes(payload.replace(b"WEBVTT", b"WEBVTX", 1))
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_size == before.st_size
        assert target.stat().st_mtime_ns == before.st_mtime_ns
    elif damage in {"marker_entry", "old_schema"}:
        document = json.loads(marker.read_bytes())
        if damage == "marker_entry":
            document["artifacts"].pop("vtt_path")
        else:
            document["schema"] = "archive-bundle-v1"
        marker.write_text(json.dumps(document), encoding="ascii")
    else:
        paths.pop("vtt_path")
    assert not archive.archive_bundle_complete(tmp_path, paths)
    assert not verify_bundle(tmp_path, paths)


def test_all_replacements_are_guarded_but_hashes_and_staging_are_outside(tmp_path, monkeypatch):
    entered = False
    replacements: list[str] = []
    real_replace, real_hash = archive.os.replace, archive.hashlib.sha256

    @contextmanager
    def guard(invalidate):
        nonlocal entered
        assert not entered
        entered = True
        try:
            yield
        except BaseException:
            invalidate()
            raise
        finally:
            entered = False

    def replace(source, target, **kwargs):
        assert entered
        replacements.append(Path(target).name)
        return real_replace(source, target, **kwargs)

    def digest(*args, **kwargs):
        assert not entered
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(archive.os, "replace", replace)
    monkeypatch.setattr(archive.hashlib, "sha256", digest)
    paths = _bundle(tmp_path, publication_guard=guard)
    assert replacements == ["bundle.srt", "bundle.vtt", "bundle.txt", "bundle.md", "bundle.raw.json", ".bundle-ready"]
    assert not entered
    assert archive.archive_bundle_complete(tmp_path, paths)


def test_guard_refusal_keeps_prior_bundle_and_cleans_staging(tmp_path):
    paths = _bundle(tmp_path)
    before = {path: (tmp_path / path).read_bytes() for path in paths.values()}

    @contextmanager
    def refuse(_invalidate):
        raise RuntimeError("cancelled")
        yield

    with pytest.raises(RuntimeError, match="cancelled"):
        _bundle(tmp_path, publication_guard=refuse, segments=[{"start": 0, "end": 1, "text": "late"}])
    assert before == {path: (tmp_path / path).read_bytes() for path in paths.values()}
    assert archive.archive_bundle_complete(tmp_path, paths)
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list((tmp_path / "transcripts").glob(".archive-bundle-stage-*"))


@pytest.mark.parametrize("fault", ["replacement", "guard_exit"])
def test_failure_after_start_removes_ready_marker_and_cleans_stage(tmp_path, monkeypatch, fault):
    paths = _bundle(tmp_path)
    real_replace = archive.os.replace

    def failing_replace(source, target, **kwargs):
        if Path(target).name == "bundle.txt":
            raise OSError("replacement failed")
        return real_replace(source, target, **kwargs)

    @contextmanager
    def failing_exit(invalidate):
        try:
            yield
            raise OSError("guard commit failed")
        except BaseException:
            invalidate()
            raise

    if fault == "replacement":
        monkeypatch.setattr(archive.os, "replace", failing_replace)
        kwargs = {}
    else:
        kwargs = {"publication_guard": failing_exit}
    with pytest.raises(OSError):
        _bundle(tmp_path, **kwargs)
    assert not archive.bundle_marker_path(tmp_path / paths["srt_path"]).exists()
    assert not archive.archive_bundle_complete(tmp_path, paths)
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list((tmp_path / "transcripts").glob(".archive-bundle-stage-*"))


def _workflow_publish(root, write_base):
    connection = open_database(root)
    try:
        with connection:
            connection.execute("INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) VALUES (1, 'test', 1, 1)")
            connection.execute("INSERT INTO videos(bvid, mid, title, pubdate, created_at, updated_at) VALUES ('BVvtt', 1, 'VTT workflow', 1, 1, 1)")
            connection.execute("""INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title, duration_ms, processing_status, created_at, updated_at)
                                  VALUES (1, 'BVvtt', 0, 1, 'p0', 8000, 'metadata_collected', 1, 1)""")
        transcripts = TranscriptRepository(connection)
        transcripts.start_acquisition_run(AcquisitionRunRecord(
            run_id="webvtt", kind="subtitle", selector_kind="bvid", selector_target="BVvtt",
            requested_limit=1, credential_present=False, started_at=1,
        ))
        stored = transcripts.record_acquired_transcript(
            run_id="webvtt", video_part_id=1, source_kind="subtitle-ai", language="zh-CN",
            segments=tuple(TranscriptSegmentRecord(round(s["start"] * 1000), round(s["end"] * 1000), s["text"]) for s in SEGMENTS),
            started_at=1, finished_at=2, created_at=2,
        )
        transcripts.finish_acquisition_run("webvtt", 2)
        workflow = WorkflowRepository(connection)
        workflow.request_publication(video_part_id=1, transcript_id=stored.transcript_id)
        job = workflow.claim("vtt-worker")
        assert job is not None
        handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=write_base, sessdata=None)
        try:
            result = handlers.publish(job)
        finally:
            handlers.close()
        workflow.finish(job.job_id, worker_id="vtt-worker", result=result, expected_attempt_count=job.attempt_count)
        return {key: result[key] for key in REQUIRED_ARTIFACT_KEYS}
    finally:
        connection.close()


@pytest.mark.parametrize("external", [False, True])
def test_workflow_publish_verify_coverage_and_export_require_vtt(tmp_path, capsys, external):
    write_base = tmp_path / "products" if external else tmp_path
    write_base.mkdir(exist_ok=True)
    paths = _workflow_publish(tmp_path, write_base)
    roots = ArtifactRoots.of(tmp_path, write_base)
    assert verify_bundle(write_base, paths)
    assert workflow_records(tmp_path, artifact_roots=roots)["BVvtt:p0"]["status"] == "archived"
    assert CoverageReport.build(tmp_path, artifact_roots=roots).data["cumulative"]["complete"] == 1
    assert IntegrityVerifier().verify(tmp_path, artifact_roots=roots).defect_count == 0
    exported = json.loads(export_records(tmp_path, "json", artifact_roots=roots))
    assert Path(exported[0]["vtt_path"]).as_posix() == paths["vtt_path"]
    assert exported[0]["status"] == "archived"
    header = next(csv.reader(io.StringIO(format_csv_export(exported))))
    assert header[header.index("srt_path") + 1] == "vtt_path"

    (write_base / paths["vtt_path"]).unlink()
    assert workflow_records(tmp_path, artifact_roots=roots)["BVvtt:p0"]["status"] == "subtitle_done"
    report = CoverageReport.build(tmp_path, artifact_roots=roots).data
    assert report["cumulative"]["complete"] == 0
    assert report["rows"][0]["artifact_present"] is False
    assert IntegrityVerifier().verify(tmp_path, artifact_roots=roots).defect_count == 1
    argv = ["verify", "--archive-root", str(tmp_path), "--format", "json"]
    if external:
        argv += ["--artifact-root", str(write_base)]
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().out)["defect_count"] == 1


def test_adoption_checks_vtt_content_even_when_digest_is_valid(tmp_path):
    from bili_asr.services.transcript_adoption import AdoptionRefused
    paths = _bundle(tmp_path)
    row = {**ENTRY, **paths, "status": "archived", "source": "subtitle-ai", "language": "zh-CN"}
    part = {"work_id": ENTRY["work_id"], "cid": ENTRY["cid"]}
    assert len(read_archived_transcript(row, part, tmp_path).segments) == 2
    target = tmp_path / paths["vtt_path"]
    target.write_text("WEBVTT\n\n", encoding="utf-8")
    marker = archive.bundle_marker_path(tmp_path / paths["srt_path"])
    document = json.loads(marker.read_bytes())
    document["artifacts"]["vtt_path"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    marker.write_text(json.dumps(document), encoding="ascii")
    with pytest.raises(AdoptionRefused, match="bundle_content_mismatch"):
        read_archived_transcript(row, part, tmp_path)
