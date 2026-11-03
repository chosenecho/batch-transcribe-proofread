"""Offline contract tests for large-context proofreading and resumable rendering."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest
import responses

from bili_asr import cli
from bili_asr.deepseek import DeepSeekClient, DeepSeekError, normalize_usage, parse_response, request_body
from bili_asr.editorial import (
    EditorialConfig, RevisionValidationError, TEMPLATE_VERSION, canonical, digest,
    estimate_utf8_tokens, local_precheck, local_revision, prepare_input, render_documents, validate_revision,
)
from bili_asr.editorial_runtime import EditorialWorkflowHandlers
from bili_asr.storage import open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.models import TranscriptRecord, TranscriptSegmentRecord
from bili_asr.storage.workflow import AsrPolicy, AsrProfile, JobKind, WorkflowRepository
from bili_asr.workflow import WorkflowExecutor
from tests.test_workflow_control_plane import _seed_part


def record(texts=("这是一段原始文字", "第二段文字"), *, transcript_id=1, source="asr-local", part=1):
    return TranscriptRecord(transcript_id, part, source, "zh-CN", None, 1,
                            digest(list(texts)), 1,
                            tuple(TranscriptSegmentRecord(i * 1000, i * 1000 + 900, text)
                                  for i, text in enumerate(texts)))


def no_changes(chunk):
    return {"chunk_id": chunk["chunk_id"], "paragraphs": [
        {"segment_ids": [s["segment_id"]], "text": s["text"], "issues": []}
        for s in chunk["editable_segments"]]}


def envelope(value, *, finish="stop"):
    return {"id": "offline-call", "model": "deepseek-flash-offline",
            "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            "choices": [{"finish_reason": finish, "message": {"content": canonical(value)}}]}


def insert_record(connection, value, *, measured=True):
    with connection:
        connection.execute("INSERT INTO transcripts(transcript_id, video_part_id, source_kind, language, "
                           "model_id, version, content_sha256, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                           (value.transcript_id, value.video_part_id, value.source_kind, value.language,
                            value.version, value.content_sha256, value.created_at))
        for i, s in enumerate(value.segments):
            connection.execute("INSERT INTO transcript_segments VALUES (?, ?, ?, ?, ?)",
                               (value.transcript_id, i, s.start_ms, s.end_ms, s.text))
        if measured and value.source_kind == "asr-local":
            from tests.support.asr_quality import measured_fixture_evidence
            measured_fixture_evidence(connection, value)


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    insert_record(connection, record())
    try:
        yield connection
    finally:
        connection.close()


def test_large_context_keeps_an_entire_long_transcript_in_one_chunk():
    base = record(tuple("中文讲述 " * 25 for _ in range(200)))
    prepared = prepare_input(base, None, EditorialConfig(max_chunk_chars=100_000))
    assert len(prepared["chunks"]) == 1
    assert len(prepared["chunks"][0]["editable_segments"]) == 200
    assert prepared["input_id"] == digest(prepared["snapshot"])
    assert prepare_input(base, None, EditorialConfig(max_chunk_chars=100_000)) == prepared


def test_output_reserve_can_split_text_that_fits_the_input_context():
    base = record(tuple("中文讲述 " * 60 for _ in range(200)))
    assert len(prepare_input(base, None, EditorialConfig(max_chunk_chars=100_000))["chunks"]) == 2


def test_budgeted_chunks_assign_each_segment_once_with_readonly_neighbors():
    base = record(tuple("中文讲述 " * 10 for _ in range(12)))
    config = EditorialConfig(max_output_tokens=2500, context_segments=1)
    prepared = prepare_input(base, None, config)
    assert len(prepared["chunks"]) > 1
    ids = [s["segment_id"] for c in prepared["chunks"] for s in c["editable_segments"]]
    assert ids == [f"t1:s{i}" for i in range(12)]
    assert prepared["chunks"][0]["readonly_context"]
    assert prepared["chunks"][1]["readonly_context"][0]["segment_id"] in ids


def test_local_precheck_only_normalizes_safe_formatting_and_flags_semantics():
    assert local_precheck("  这是一句。。  ") == {
        "flags": ["format_whitespace", "repeated_punctuation"],
        "safe": True,
        "safe_text": "这是一句。",
        "actions": ["normalize_whitespace", "collapse_repeated_punctuation"],
    }
    flagged = local_precheck("就是这个这个方案")
    assert flagged["safe"] is False
    assert "possible_filler" in flagged["flags"]
    assert "possible_repetition" in flagged["flags"]
    assert flagged["safe_text"] is None


def test_local_revision_only_handles_all_mechanical_segments():
    chunk = prepare_input(record(("  这是一句。。  ",)), None, EditorialConfig())["chunks"][0]
    blocks = local_revision(chunk)
    assert blocks is not None
    assert blocks[0]["text"] == "这是一句。"
    semantic = prepare_input(record(("就是这个这个方案",)), None, EditorialConfig())["chunks"][0]
    assert local_revision(semantic) is None


def test_chunk_budget_exposes_component_estimates_without_changing_frozen_identity():
    prepared = prepare_input(record(("中文" * 30, "第二段")), None, EditorialConfig())
    budget = prepared["chunks"][0]["budget_estimate"]
    assert budget["method"] == "serialized-request-utf8/3-v1"
    assert budget["system_prompt_tokens"] == estimate_utf8_tokens(
        prepared["snapshot"]["system_prompt"])
    assert budget["editable_tokens"] > 0
    assert budget["request_tokens"] >= budget["system_prompt_tokens"]
    assert prepared == prepare_input(record(("中文" * 30, "第二段")), None, EditorialConfig())


def test_reference_alignment_preserves_unassigned_reference_evidence():
    reference = record(("字幕一", "字幕二", "未匹配"), transcript_id=2, source="subtitle-ai")
    prepared = prepare_input(record(), reference, EditorialConfig())
    assert [s["segment_id"] for s in prepared["chunks"][0]["reference_segments"]] == ["t2:s0", "t2:s1"]
    assert prepared["snapshot"]["unassigned_reference_ids"] == ["t2:s2"]


@pytest.mark.parametrize("config", [
    {"context_tokens": 1000}, {"max_output_tokens": 393217}, {"context_tokens": 1048577},
    {"max_input_tokens": -1}, {"timeout_seconds": 0}, {"context_segments": 21},
    {"base_url": "http://example.test"}, {"thinking_mode": "invalid"},
    {"reasoning_effort": "invalid"},
    {"top_p": 0.8}, {"top_p": 1.1}, {"top_p": True}, {"top_p": float("nan")},
])
def test_invalid_model_budgets_fail_before_network(config):
    with pytest.raises(ValueError):
        EditorialConfig(**config)


def test_single_oversized_segment_fails_explicitly():
    with pytest.raises(ValueError, match="one segment"):
        prepare_input(record(("中" * 10000,)), None, EditorialConfig(max_output_tokens=2000))


def test_cross_part_reference_is_rejected():
    with pytest.raises(ValueError, match="same part"):
        prepare_input(record(), record(transcript_id=2, source="subtitle-ai", part=2), EditorialConfig())


def test_qwen_language_names_match_caption_codes_and_preserve_original_labels(database):
    with database:
        database.execute("UPDATE transcripts SET language = 'Chinese' WHERE transcript_id = 1")
    caption = replace(record(transcript_id=2, source="subtitle-ai"), language="ai-zh")
    insert_record(database, caption)
    repository = EditorialRepository(database)
    assert repository.latest_sources(1) == (1, 2)
    prepared = repository.prepare(1, 2, EditorialConfig())
    assert prepared["snapshot"]["base"]["language"] == "Chinese"
    assert prepared["snapshot"]["reference"]["language"] == "ai-zh"
    with pytest.raises(ValueError, match="same part and language"):
        prepare_input(record(), replace(caption, language="en"), EditorialConfig())
    with pytest.raises(ValueError, match="same part and language"):
        prepare_input(record(), replace(caption, language="zh-TW"), EditorialConfig())


def test_adjacent_fragments_merge_into_readable_prose_with_original_provenance():
    base = record(("就是呃对于民众的生活以及政治观点就塑造力的那些渠道平", "台还是掌握在他们手里"))
    chunk = prepare_input(base, None, EditorialConfig())["chunks"][0]
    text = "那些能够塑造民众生活和政治观点的渠道、平台，仍然掌握在他们手里。"
    candidate = {"chunk_id": chunk["chunk_id"], "paragraphs": [{
        "segment_ids": ["t1:s0", "t1:s1"], "text": text, "issues": []}]}
    blocks = validate_revision(chunk, candidate)
    assert blocks[0]["text"] == text
    assert blocks[0]["original_text"] == "".join(s.text for s in base.segments)
    assert blocks[0]["end_ms"] == base.segments[-1].end_ms
    assert chunk["editable_segments"][0]["text"] == base.segments[0].text


def test_unresolved_issue_does_not_revert_surrounding_sentence_cleanup():
    chunk = prepare_input(record(("呃这个名词可能是三个人就是这样",)), None, EditorialConfig())["chunks"][0]
    value = no_changes(chunk)
    value["paragraphs"][0].update(text="这个名词可能是三个人，情况就是这样。", issues=[
        {"note": "名词识别不确定", "candidate": "", "evidence_refs": ["t1:s0"]}])
    block = validate_revision(chunk, value)[0]
    assert block["text"] == value["paragraphs"][0]["text"]
    assert block["issues"]


@pytest.mark.parametrize("defect", ["missing", "duplicate", "reorder", "unknown", "extra", "empty", "reference"])
def test_invalid_revision_cannot_silently_drop_or_invent_source_coverage(defect):
    chunk = prepare_input(record(), None, EditorialConfig())["chunks"][0]
    value = no_changes(chunk)
    paragraphs = value["paragraphs"]
    if defect == "missing":
        paragraphs.pop()
    elif defect == "duplicate":
        paragraphs[1] = deepcopy(paragraphs[0])
    elif defect == "reorder":
        paragraphs.reverse()
    elif defect == "unknown":
        paragraphs[0]["segment_ids"] = ["invented"]
    elif defect == "extra":
        paragraphs[0]["heading"] = "话题标题"
    elif defect == "empty":
        paragraphs[0]["text"] = " "
    elif defect == "reference":
        paragraphs[0]["issues"] = [{"note": "不确定", "candidate": "", "evidence_refs": ["nonexistent"]}]
    with pytest.raises(RevisionValidationError):
        validate_revision(chunk, value)


@pytest.mark.parametrize("text", ["# 话题标题", "00:00:01 正文", "[00:00:01] 正文", "第一行\n第二行", "- 列表"])
def test_model_cannot_insert_headings_timestamps_or_lists_in_reading_paragraph(text):
    chunk = prepare_input(record(), None, EditorialConfig())["chunks"][0]
    value = no_changes(chunk)
    value["paragraphs"][0]["text"] = text
    with pytest.raises(RevisionValidationError):
        validate_revision(chunk, value)


def test_issue_reference_must_overlap_the_paragraph_sources():
    chunk = prepare_input(record(), record(transcript_id=2, source="subtitle-ai"), EditorialConfig())["chunks"][0]
    assert chunk["editable_segments"][0]["allowed_issue_refs"] == ["t1:s0", "t2:s0"]
    assert chunk["editable_segments"][1]["allowed_issue_refs"] == ["t1:s1", "t2:s1"]
    value = no_changes(chunk)
    value["paragraphs"][0]["issues"] = [{"note": "不确定", "candidate": "", "evidence_refs": ["t2:s1"]}]
    with pytest.raises(RevisionValidationError, match="unrelated"):
        validate_revision(chunk, value)


def test_readonly_neighbors_cannot_become_output_sources():
    prepared = prepare_input(record(tuple("一段文字" for _ in range(8))), None,
                             EditorialConfig(max_output_tokens=2400))
    chunk = prepared["chunks"][0]
    value = no_changes(chunk)
    value["paragraphs"][-1]["segment_ids"] = [chunk["readonly_context"][0]["segment_id"]]
    with pytest.raises(RevisionValidationError):
        validate_revision(chunk, value)


def test_thinking_and_sampling_settings_are_frozen_in_input_identity():
    first = prepare_input(record(), None, EditorialConfig())
    second = prepare_input(record(), None, EditorialConfig(top_p=1.0))
    third = prepare_input(record(), None, EditorialConfig(reasoning_effort="max"))
    assert len({x["input_id"] for x in (first, second, third)}) == 3
    assert first["snapshot"]["config"]["reasoning_effort"] == "high"
    assert first["snapshot"]["config"]["top_p"] == 0.95


def test_thinking_mode_is_configurable_and_frozen_with_default_enabled():
    enabled = EditorialConfig()
    disabled = EditorialConfig(thinking_mode="disabled", reasoning_effort="max")
    first = prepare_input(record(), None, enabled)
    second = prepare_input(record(), None, disabled)
    assert enabled.thinking_mode == "enabled"
    assert len({first["input_id"], second["input_id"]}) == 2
    assert second["snapshot"]["config"]["thinking_mode"] == "disabled"
    chunk = second["chunks"][0]
    assert request_body(chunk, disabled)["thinking"] == {"type": "disabled"}


def test_normalize_usage_accepts_provider_optional_fields_and_rejects_bad_latency():
    value = normalize_usage({
        "id": "req-123", "model": "deepseek-flash",
        "usage": {
            "prompt_tokens": 10, "completion_tokens": 20,
            "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
    }, latency_ms=321)
    assert value == {
        "provider_request_id": "req-123", "model": "deepseek-flash",
        "prompt_tokens": 10, "completion_tokens": 20,
        "total_tokens": None, "cached_tokens": None,
        "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
        "reasoning_tokens": 7, "latency_ms": 321,
    }
    assert normalize_usage({"usage": {"prompt_tokens": -1, "completion_tokens": True}})["prompt_tokens"] is None
    with pytest.raises(ValueError, match="latency_ms"):
        normalize_usage({}, latency_ms=-1)


@pytest.mark.parametrize("value", [
    {}, {"choices": []},
    {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"x":1,"x":2}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"x":NaN}'}}]},
])
def test_bad_envelopes_are_rejected(value):
    with pytest.raises(DeepSeekError):
        parse_response(value)


@responses.activate
def test_official_http_adapter_uses_json_mode_and_never_persists_auth_in_request():
    chunk = prepare_input(record(), None, EditorialConfig())["chunks"][0]
    config = EditorialConfig()
    request = request_body(chunk, config)
    responses.post("https://api.deepseek.com/chat/completions", json=envelope(no_changes(chunk)))
    client = DeepSeekClient("offline-secret")
    try:
        result = client.complete(request, config)
    finally:
        client.close()
    assert parse_response(result) == no_changes(chunk)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["model"] == "deepseek-flash"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"
    assert sent["top_p"] == 0.95
    assert "temperature" not in sent
    assert "offline-secret" not in canonical(request)


@responses.activate
@pytest.mark.parametrize("status", [401, 429, 503, 302])
def test_http_failure_is_bounded_and_not_retried_in_client(status):
    responses.post("https://api.deepseek.com/chat/completions", status=status, body="offline-secret")
    client = DeepSeekClient("offline-secret")
    try:
        with pytest.raises(DeepSeekError, match=f"HTTP {status}") as error:
            client.complete({}, EditorialConfig())
    finally:
        client.close()
    assert "offline-secret" not in str(error.value)
    assert len(responses.calls) == 1


def test_missing_key_does_not_make_network_request(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = DeepSeekClient()
    try:
        with pytest.raises(DeepSeekError, match="not configured"):
            client.complete({}, EditorialConfig())
    finally:
        client.close()


@pytest.mark.parametrize("endpoint_path", ["inference/go/v1", "zen/go/v1"])
@responses.activate
def test_opencode_endpoint_uses_own_key_and_stable_session(monkeypatch, endpoint_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "go-secret")
    endpoint = f"https://opencode.ai/{endpoint_path}"
    config = EditorialConfig(model="deepseek-v4.1-flash",
                             base_url=endpoint + "/")
    chunk = prepare_input(record(), None, config)["chunks"][0]
    request = request_body(chunk, config)
    responses.post(endpoint + "/chat/completions", json=envelope(no_changes(chunk)))
    responses.post("https://api.deepseek.com/chat/completions", json=envelope(no_changes(chunk)))
    client = DeepSeekClient()
    try:
        for _ in range(2):
            assert parse_response(client.complete(request, config)) == no_changes(chunk)
        client.complete({}, EditorialConfig())
    finally:
        client.close()
    first, second, direct = [call.request for call in responses.calls]
    assert first.headers["Authorization"] == "Bearer go-secret"
    assert first.headers["User-Agent"].startswith("bilibili-asr-archive/")
    assert first.headers["x-opencode-session"] == second.headers["x-opencode-session"]
    assert direct.headers["Authorization"] == "Bearer direct-secret"
    assert "x-opencode-session" not in direct.headers
    assert json.loads(first.body)["model"] == "deepseek-v4.1-flash"
    assert "go-secret" not in canonical(request)


@responses.activate
def test_opencode_requires_own_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-secret")
    client = DeepSeekClient()
    try:
        with pytest.raises(DeepSeekError, match="OPENCODE_API_KEY is not configured"):
            client.complete({}, EditorialConfig(base_url="https://opencode.ai/inference/go/v1"))
        assert not responses.calls
    finally:
        client.close()


class FakeClient:
    def __init__(self, *, fail_call=None):
        self.requests = []
        self.fail_call = fail_call

    def complete(self, request, config):
        self.requests.append(request)
        if len(self.requests) == self.fail_call:
            raise DeepSeekError("offline simulated interruption")
        chunk = json.loads(request["messages"][1]["content"])
        return envelope(no_changes(chunk))


def runtime(connection, root, client):
    workflow = WorkflowRepository(connection)
    repository = EditorialRepository(connection)
    handlers = EditorialWorkflowHandlers(repository, workflow, archive_root=root, client=client)
    executor = WorkflowExecutor(workflow, worker_id="offline-worker", handlers=handlers.handlers(),
                                kinds=(JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT))
    return workflow, repository, handlers, executor


def test_end_to_end_offline_freezes_inputs_renders_and_rerenders_without_model(database, tmp_path):
    fake = FakeClient()
    workflow, repository, handlers, executor = runtime(database, tmp_path, fake)
    prepared = repository.prepare(1, None, EditorialConfig())
    ids = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])[:2] == ids[:2]
    raw_before = [tuple(r) for r in database.execute("SELECT * FROM transcript_segments")]
    with database:
        database.execute("UPDATE videos SET title = 'changed after snapshot'")
    summary = executor.run()
    assert summary.succeeded == 2 and summary.failed == 0
    revision = repository.revision_for_job(ids[0])
    paths = list((tmp_path / "documents").rglob("*.md"))
    assert {p.name for p in paths} == {"ai-draft.md", "review.md"}
    assert {p.parent.name for p in paths} == {"ai-draft-v1"}
    before = {p: p.read_bytes() for p in paths}
    assert b"changed after snapshot" not in next(p for p in paths if p.name == "review.md").read_bytes()
    assert raw_before == [tuple(r) for r in database.execute("SELECT * FROM transcript_segments")]
    assert len(fake.requests) == 1
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(database.execute("SELECT * FROM editorial_inputs").fetchall()) == 1
    assert len(database.execute("SELECT * FROM document_artifacts").fetchall()) == 2
    assert {tuple(row) for row in database.execute(
        "SELECT artifact_name, manuscript_role FROM document_artifacts")} == {
        ("ai-draft.md", "ai-draft"), ("review.md", "review-reference")}
    assert database.execute("SELECT COUNT(*) FROM publication_editions").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 0
    call = database.execute("SELECT * FROM editorial_model_calls").fetchone()
    assert json.loads(call["response_json"])["usage"]["prompt_tokens"] == 100
    workflow.request_document(video_part_id=1, revision_id=revision, template_version=TEMPLATE_VERSION)
    for p in paths:
        p.unlink()
    assert executor.run().succeeded == 1
    assert before == {p: p.read_bytes() for p in paths}
    assert len(fake.requests) == 1


def test_partial_failure_resumes_saved_chunks_after_reopening_database(database, tmp_path):
    fake = FakeClient(fail_call=2)
    workflow, repository, handlers, executor = runtime(database, tmp_path, fake)
    prepared = repository.prepare(1, None, EditorialConfig(max_output_tokens=2100, context_segments=0))
    assert len(prepared["chunks"]) == 2
    proof_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    first = executor.run()
    assert first.failed == 1 and first.succeeded == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0
    assert not (tmp_path / "documents").exists()
    newer = record(("后来出现的字幕",), transcript_id=2, source="subtitle-ai")
    insert_record(database, newer)
    # A new connection and new handlers represent a restarted worker, with no
    # in-memory progress from the first invocation.
    connection = open_database(tmp_path)
    try:
        second_client = FakeClient()
        workflow2, repo2, _, executor2 = runtime(connection, tmp_path, second_client)
        workflow2.requeue_failed(part_ids=[1])
        second = executor2.run()
        assert second.succeeded == 2 and second.failed == 0
        assert len(second_client.requests) == 1
        assert repo2.load_input(prepared["input_id"])["snapshot"]["reference"] is None
        assert repo2.revision_for_job(proof_id)
        assert connection.execute("SELECT COUNT(*) FROM editorial_model_calls").fetchone()[0] == 3
    finally:
        connection.close()


def test_automatic_plan_waits_for_asr(database, tmp_path):
    workflow, repository, _, executor = runtime(database, tmp_path, FakeClient())
    profile = workflow.register_profile(AsrProfile("offline", "offline-model"))
    plan = workflow.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile, editorial_config=EditorialConfig().to_dict())
    assert plan.proofread_jobs == plan.document_jobs == 1
    assert executor.run().idle
    asr = workflow.claim("producer", kinds=(JobKind.ASR,))
    workflow.finish(asr.job_id, worker_id="producer", result={"transcript_id": 1, "run_id": "editorial-fixture-1"})
    assert executor.run().succeeded == 2
    assert database.execute("SELECT COUNT(*) FROM editorial_job_inputs").fetchone()[0] == 1


def test_stale_worker_cannot_commit_chunk_or_renew_lease(database):
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    job = workflow.claim("old", kinds=(JobKind.PROOFREAD,))
    with database:
        database.execute("UPDATE workflow_jobs SET lease_expires_at = 0 WHERE job_id = ?", (job.job_id,))
    next_job = workflow.claim("new", kinds=(JobKind.PROOFREAD,))
    assert next_job.attempt_count == job.attempt_count + 1
    with pytest.raises(RuntimeError, match="lost"):
        workflow.renew_lease(job, lease_seconds=100)
    with pytest.raises(RuntimeError, match="lost"):
        repository.begin_call(job, prepared["input_id"], prepared["chunks"][0]["chunk_id"], {})


def test_reused_worker_id_cannot_finish_or_fail_a_new_attempt(database):
    workflow = WorkflowRepository(database)
    prepared = EditorialRepository(database).prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    old = workflow.claim("same-worker", kinds=(JobKind.PROOFREAD,))
    with database:
        database.execute("UPDATE workflow_jobs SET lease_expires_at = 0 WHERE job_id = ?", (old.job_id,))
    new = workflow.claim("same-worker", kinds=(JobKind.PROOFREAD,))
    assert new.attempt_count == old.attempt_count + 1
    with pytest.raises(RuntimeError, match="lost"):
        workflow.finish(old.job_id, worker_id="same-worker", expected_attempt_count=old.attempt_count)
    with pytest.raises(RuntimeError, match="lost"):
        workflow.fail(old.job_id, worker_id="same-worker", error_code="old-error",
                      expected_attempt_count=old.attempt_count)
    assert database.execute("SELECT status FROM workflow_jobs WHERE job_id = ?", (new.job_id,)).fetchone()[0] == "running"


def test_invalid_model_response_is_audited_but_never_committed(database, tmp_path):
    class InvalidClient(FakeClient):
        def complete(self, request, config):
            chunk = json.loads(request["messages"][1]["content"])
            candidate = no_changes(chunk)
            candidate["paragraphs"].pop()
            return envelope(candidate)

    workflow, repository, _, executor = runtime(database, tmp_path, InvalidClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    call = database.execute("SELECT * FROM editorial_model_calls").fetchone()
    assert json.loads(call["response_json"])["id"] == "offline-call"
    assert call["error_code"] == "RevisionValidationError"
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0
    assert not (tmp_path / "documents").exists()


def test_render_escapes_transcript_markdown_and_marks_unresolved_edits():
    prepared = prepare_input(record(("<script>执行</script> [链接](evil)",)), None, EditorialConfig())
    chunk = prepared["chunks"][0]
    candidate = no_changes(chunk)
    candidate["paragraphs"][0]["issues"] = [{"note": "待核对", "candidate": "候选", "evidence_refs": []}]
    blocks = validate_revision(chunk, candidate)
    output = render_documents({"title": "离线测试", "bvid": "BV1o24y157iQ", "page_index": 0}, prepared, blocks, "revision")
    assert "<script>" not in output["ai-draft.md"]
    assert "&lt;script&gt;" in output["ai-draft.md"]
    assert "[q1-1]" not in output["ai-draft.md"]
    assert "[^q1-1]" not in output["ai-draft.md"]
    assert "未经人工复核" not in output["ai-draft.md"]
    assert "待核对" in output["review.md"]
    assert "校验参照稿件" in output["review.md"]
    assert "00:00:00" not in output["ai-draft.md"]
    assert "00:00:00" in output["review.md"]
    assert not output["ai-draft.md"].startswith("#")


@pytest.mark.parametrize("template", ["reading-v2", "unknown-template"])
def test_unsupported_template_is_rejected_before_planner_writes(database, template):
    workflow = WorkflowRepository(database)
    before = database.execute("SELECT * FROM workflow_jobs").fetchall()
    with pytest.raises(ValueError, match="unsupported document template"):
        workflow.request_document(video_part_id=1, revision_id="uncommitted", template_version=template)
    assert database.execute("SELECT * FROM workflow_jobs").fetchall() == before
    assert not database.in_transaction


@pytest.mark.parametrize("artifact_name", ["ai-draft.md", "review.md"])
def test_render_registration_conflict_preserves_the_entire_pair(database, tmp_path, artifact_name):
    fake = FakeClient()
    workflow, repository, _, executor = runtime(database, tmp_path, fake)
    prepared = repository.prepare(1, None, EditorialConfig())
    proof_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision = repository.revision_for_job(proof_id)
    before = {path: path.read_bytes() for path in (tmp_path / "documents").rglob("*.md")}
    with database:
        database.execute("UPDATE document_artifacts SET content_sha256 = ? WHERE artifact_name = ?",
                         ("0" * 64, artifact_name))
    registered_before = [tuple(row) for row in database.execute("SELECT * FROM document_artifacts")]
    workflow.request_document(video_part_id=1, revision_id=revision, template_version=TEMPLATE_VERSION)
    summary = executor.run()
    assert summary.failed == 1 and summary.succeeded == 0
    assert before == {path: path.read_bytes() for path in before}
    assert registered_before == [tuple(row) for row in database.execute("SELECT * FROM document_artifacts")]
    assert len(fake.requests) == 1


@pytest.mark.parametrize("artifact_name", ["ai-draft.md", "review.md"])
def test_render_existing_file_conflict_preserves_files_and_registration(database, tmp_path, artifact_name):
    workflow, repository, _, executor = runtime(database, tmp_path, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    proof_id, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision = repository.revision_for_job(proof_id)
    target = next(path for path in (tmp_path / "documents").rglob(artifact_name))
    target.write_text("unexpected replacement", encoding="utf-8")
    before = {path: path.read_bytes() for path in (tmp_path / "documents").rglob("*.md")}
    registered_before = [tuple(row) for row in database.execute("SELECT * FROM document_artifacts")]
    workflow.request_document(video_part_id=1, revision_id=revision, template_version=TEMPLATE_VERSION)
    assert executor.run().failed == 1
    assert before == {path: path.read_bytes() for path in before}
    assert registered_before == [tuple(row) for row in database.execute("SELECT * FROM document_artifacts")]


def test_render_rejects_linked_artifact_parent_before_writing(database, tmp_path):
    workflow, repository, _, executor = runtime(database, tmp_path, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run(limit=1).succeeded == 1
    outside = tmp_path / "unmanaged"
    outside.mkdir()
    try:
        (tmp_path / "documents").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    assert executor.run().failed == 1
    assert list(outside.iterdir()) == []
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("target", ["input", "revision"])
def test_render_rejects_changed_frozen_identity_before_artifact_writes(database, tmp_path, target):
    workflow, repository, _, executor = runtime(database, tmp_path, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run(limit=1).succeeded == 1
    with database:
        if target == "input":
            changed = deepcopy(prepared)
            changed["snapshot"]["metadata"]["title"] = "changed fixed metadata"
            database.execute("UPDATE editorial_inputs SET prepared_json = ?", (canonical(changed),))
        else:
            database.execute("UPDATE editorial_revisions SET blocks_json = '[]'")
    assert executor.run().failed == 1
    assert not (tmp_path / "documents").exists()
    assert database.execute("SELECT COUNT(*) FROM document_artifacts").fetchone()[0] == 0


def test_cli_proofread_render_and_error_paths(database, tmp_path, monkeypatch, capsys):
    fake = FakeClient()
    monkeypatch.setattr(DeepSeekClient, "complete", fake.complete)
    root = str(tmp_path)
    assert cli.main(["workflow", "proofread", "--archive-root", root, "--part-id", "1",
                     "--reasoning-effort", "high", "--top-p", "0.95"]) == 0
    assert "chunks=1" in capsys.readouterr().out
    assert cli.main(["workflow", "run", "--archive-root", root, "--only-editorial"]) == 0
    assert "succeeded=2" in capsys.readouterr().out
    revision = database.execute("SELECT revision_id FROM editorial_revisions").fetchone()[0]
    assert cli.main(["workflow", "render", "--archive-root", root, "--revision-id", revision]) == 0
    assert cli.main(["workflow", "run", "--archive-root", root, "--only-editorial"]) == 0
    assert len(fake.requests) == 1
    assert fake.requests[0]["thinking"] == {"type": "enabled"}
    assert fake.requests[0]["reasoning_effort"] == "high"
    assert fake.requests[0]["top_p"] == 0.95
    assert cli.main(["workflow", "proofread", "--archive-root", root, "--part-id", "99"]) == 1
