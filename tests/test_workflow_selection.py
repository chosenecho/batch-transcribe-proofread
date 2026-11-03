"""Stored BVID selection uses the existing workflow planner's contract."""

from __future__ import annotations

import json

import pytest

from bili_asr import cli
from bili_asr.storage import open_database
from bili_asr.storage.workflow_selection import resolve_workflow_selection


def _seed(connection) -> None:
    with connection:
        connection.execute(
            "INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) VALUES (1, 'test', 1, 1)"
        )
        connection.executemany(
            "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) "
            "VALUES (?, ?, 1, 'title', 1, 1, 1)",
            [("BVb", 1), ("BVa", 2), ("BVempty", 3), ("BVgone", 4)],
        )
        connection.executemany(
            "INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title, duration_ms, "
            "processing_status, created_at, updated_at) VALUES (?, ?, ?, ?, 'part', 1000, ?, 1, 1)",
            [(9, "BVb", 0, 1, "metadata_collected"),
             (4, "BVa", 1, 2, "metadata_collected"),
             (7, "BVa", 0, 3, "metadata_collected"),
             (8, "BVa", 2, 4, "gone"),
             (11, "BVgone", 0, 5, "gone")],
        )


@pytest.fixture
def connection(tmp_path):
    result = open_database(tmp_path)
    _seed(result)
    try:
        yield result
    finally:
        result.close()


def test_bvid_resolves_all_stored_active_parts_in_stable_order_without_writes(connection):
    changes = connection.total_changes
    selection = resolve_workflow_selection(connection, bvids=["BVb", "BVa", "BVb"])
    assert selection.part_ids == (7, 4, 9)
    assert [target.work_id for target in selection.targets] == ["BVa:p0", "BVa:p1", "BVb:p0"]
    assert [target.work_id for target in selection.excluded_gone] == ["BVa:p2"]
    assert connection.total_changes == changes
    assert not connection.in_transaction


def test_index_is_zero_based_and_applies_to_every_bvid(connection):
    selection = resolve_workflow_selection(connection, bvids=["BVb", "BVa"], page_index=0)
    assert selection.part_ids == (7, 9)
    assert not selection.excluded_gone


def test_exact_ids_are_deduplicated_and_sorted_by_stored_identity(connection):
    assert resolve_workflow_selection(connection, part_ids=[9, 4, 7, 9]).part_ids == (7, 4, 9)


@pytest.mark.parametrize("kwargs,diagnostic", [
    ({"bvids": ["BVunknown"]}, "unknown BVID=BVunknown"),
    ({"bvids": ["BVempty"]}, "BVID=BVempty has no stored parts"),
    ({"bvids": ["BVgone"]}, "all are gone"),
    ({"bvids": ["BVa"], "page_index": 2}, "BVID=BVa page_index=2 is gone"),
    ({"bvids": ["BVa"], "page_index": 3}, "BVID=BVa page_index=3 has no stored part"),
    ({"part_ids": [8]}, "video_part_id=8 (BVa:p2) is gone"),
    ({"part_ids": [99]}, "unknown video_part_id=99"),
    ({"part_ids": [4], "page_index": 0}, "--page-index requires --bvid"),
    ({"bvids": ["BVa"], "page_index": -1}, "non-negative"),
    ({"bvids": ["BVa"], "part_ids": [4]}, "without mixing"),
    ({"bvids": [""]}, "non-empty"),
    ({"part_ids": [0]}, "positive integers"),
    ({}, "select either"),
])
def test_unresolved_and_invalid_selection_has_explicit_diagnostic(connection, kwargs, diagnostic):
    with pytest.raises(ValueError) as exc:
        resolve_workflow_selection(connection, **kwargs)
    assert diagnostic in str(exc.value)


def test_missing_pairs_are_reported_together_with_no_partial_result(connection):
    with pytest.raises(ValueError) as exc:
        resolve_workflow_selection(connection, bvids=["BVb", "BVa", "BVunknown"], page_index=1)
    assert "BVID=BVb page_index=1 has no stored part" in str(exc.value)
    assert "unknown BVID=BVunknown" in str(exc.value)


def test_selector_chunks_many_bvids_and_exact_ids(connection):
    rows = [(f"BVscale{i:04d}", i + 1000) for i in range(901)]
    with connection:
        connection.executemany(
            "INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) "
            "VALUES (?, ?, 1, 'scale', 1, 1, 1)", rows,
        )
        connection.executemany(
            "INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title, duration_ms, "
            "processing_status, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, 'scale', 1000, 'metadata_collected', 1, 1)",
            [(aid, bvid, aid) for bvid, aid in rows],
        )
    queries = []
    connection.set_trace_callback(queries.append)
    try:
        result = resolve_workflow_selection(connection, bvids=[bvid for bvid, _ in rows])
        assert len(result.targets) == 901
        assert len(queries) == 4
        queries.clear()
        assert resolve_workflow_selection(connection, part_ids=[aid for _, aid in rows]).part_ids == result.part_ids
        assert len(queries) == 2
    finally:
        connection.set_trace_callback(None)


def _plan(root, *selection, extra=()):
    return cli.main(["workflow", "plan", "--archive-root", str(root), *selection,
                     "--device", "cpu", *extra])


def test_cli_summary_includes_resolved_targets_exclusions_and_idempotent_counts(connection, tmp_path, capsys):
    assert _plan(tmp_path, "--bvid", "BVb", "--bvid", "BVa", "--bvid", "BVa") == 0
    first = capsys.readouterr()
    assert "subtitle=3 audio=3 asr=3" in first.out
    assert "target: bvid=BVa page_index=0 video_part_id=7 work_id=BVa:p0" in first.out
    assert "target: bvid=BVa page_index=1 video_part_id=4 work_id=BVa:p1" in first.out
    assert "excluded gone: bvid=BVa page_index=2 video_part_id=8 work_id=BVa:p2" in first.out
    assert first.out.index("work_id=BVa:p0") < first.out.index("work_id=BVa:p1") < first.out.index("work_id=BVb:p0")
    assert _plan(tmp_path, "--bvid", "BVa", "--bvid", "BVb") == 0
    second = capsys.readouterr()
    assert "subtitle=0 audio=0 asr=0" in second.out
    assert "work_id=BVa:p0" in second.out and "work_id=BVb:p0" in second.out
    assert connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 9
    assert connection.execute("SELECT COUNT(*) FROM workflow_asr_profiles").fetchone()[0] == 1


@pytest.mark.parametrize("args,diagnostic", [
    (("--bvid", "BVa", "--bvid", "BVmissing"), "BVmissing"),
    (("--bvid", "BVa", "--bvid", "BVb", "--page-index", "1"), "BVb page_index=1"),
    (("--part-id", "7", "--part-id", "99"), "video_part_id=99"),
    (("--part-id", "7", "--page-index", "0"), "requires --bvid"),
    (("--bvid", "BVa", "--proofread", "--context-tokens", "1"), "context window"),
])
def test_cli_selection_and_configuration_failure_does_not_create_profile_or_jobs(connection, tmp_path, capsys, args, diagnostic):
    assert _plan(tmp_path, *args) == 1
    result = capsys.readouterr()
    assert diagnostic in result.err
    assert not result.out
    assert connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM workflow_asr_profiles").fetchone()[0] == 0


@pytest.mark.parametrize("args", [
    ("--bvid", "BVa", "--part-id", "7"),
    ("--bvid", "BVa", "--page-index", "-1"),
    ("--bvid", "BVa", "--page-index", "not-an-index"),
    ("--bvid", "BVa", "--asr-policy", "below-threshold"),
    ("--bvid", "BVa", "--quality-threshold", "0.5"),
    (),
])
def test_invalid_parser_selector_is_rejected_before_database_creation(tmp_path, args):
    with pytest.raises(SystemExit) as exc:
        _plan(tmp_path, *args)
    assert exc.value.code != 0
    assert not (tmp_path / "archive.db").exists()


def _scheduled_contract(connection):
    rows = connection.execute(
        "SELECT job_id, kind, video_part_id, profile_id, policy_key, payload_json FROM workflow_jobs ORDER BY kind"
    ).fetchall()
    # Independently initialized archives have different job UUIDs.  Compare
    # the referenced job identities and payloads, not those generated UUIDs.
    identities = {row["job_id"]: f"{row['kind']}:{row['video_part_id']}" for row in rows}
    jobs = []
    for row in rows:
        job = dict(row)
        del job["job_id"]
        payload = json.loads(job["payload_json"])
        for key in ("asr_job_id", "proofread_job_id"):
            if key in payload:
                payload[key] = identities[payload[key]]
        job["payload_json"] = json.dumps(payload, sort_keys=True)
        jobs.append(job)
    dependencies = [tuple(row) for row in connection.execute(
        "SELECT j.kind, p.kind FROM workflow_job_dependencies d "
        "JOIN workflow_jobs j ON j.job_id = d.job_id "
        "JOIN workflow_jobs p ON p.job_id = d.prerequisite_job_id ORDER BY j.kind, p.kind"
    )]
    return jobs, dependencies


@pytest.mark.parametrize("policy", ["all", "selected"])
def test_bvid_and_part_id_selection_preserve_profile_policy_and_proofread_contract(tmp_path, capsys, policy):
    results = []
    for name, selector in [("bvid", ("--bvid", "BVa", "--page-index", "0")),
                           ("part", ("--part-id", "7"))]:
        root = tmp_path / name
        connection = open_database(root)
        try:
            _seed(connection)
            with connection:
                connection.execute(
                    "INSERT INTO workflow_quality_assessments(video_part_id, source_kind, score, assessor, details_json, assessed_at) "
                    "VALUES (7, 'subtitle-ai', 0.2, 'test', '{}', 1)"
                )
            extra = ("--asr-policy", policy, "--proofread",
                     "--profile-key", "selection-profile", "--model", "model-v1",
                     "--model-revision", "abc", "--language", "Chinese")
            assert _plan(root, *selector, extra=extra) == 0
            assert "proofread=1 documents=1" in capsys.readouterr().out
            contract = _scheduled_contract(connection)
            assert len(contract[0]) == 5
            asr = next(job for job in contract[0] if job["kind"] == "asr")
            assert asr["policy_key"] == policy
            assert json.loads(asr["payload_json"])["reference_transcript_id"] is None
            profile = connection.execute("SELECT profile_key, model_name, model_revision, language FROM workflow_asr_profiles").fetchone()
            assert tuple(profile) == ("selection-profile", "model-v1", "abc", "Chinese")
            assert _plan(root, *selector, extra=extra) == 0
            assert "proofread=0 documents=0" in capsys.readouterr().out
            assert connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 5
            results.append(contract)
        finally:
            connection.close()
    assert results[0] == results[1]
