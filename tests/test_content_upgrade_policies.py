"""Registered content policies retain original evidence and derive only known facts."""
from contextlib import closing
import hashlib
from pathlib import Path

import pytest

from bili_asr import cli
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.contracts.content_policies import CONTENT_POLICIES, content_policy
from bili_asr.contracts.registry import LEGACY_FACTS_POLICY, SOURCE_SUPPLEMENT_POLICY
from bili_asr.publication import get_edition
from bili_asr.services.archive_upgrade import apply_upgrade, plan_upgrade
from bili_asr.services.preserved_body_import import plan_preserved_body_import, apply_preserved_body_import
from bili_asr.services.transcript_derivatives import export_transcript_vtt
from bili_asr.storage.database import connect_database
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_formal_upgrade_and_registered_content_conversion_keep_original_review_and_body(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    facts = frozen_archive(source)
    apply_upgrade(plan_upgrade(source, target, target_contracts=(
        "universal-v2", "preserved-body-import-v1", SOURCE_SUPPLEMENT_POLICY)))
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        protected = ("editorial_inputs", "editorial_revisions", "editorial_model_calls", "workflow_jobs",
                     "workflow_attempts", "publication_edition_reviews", "publication_releases")
        before = {name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                  for name in protected}
        old = get_edition(connection, facts["ids"]["edition_current"])
        plan = plan_preserved_body_import(connection, selectors=[{"kind": "edition", "id": old["edition_id"]}],
                                         artifact_roots=(target,))
        result = apply_preserved_body_import(connection, plan=plan, artifact_roots=(target,),
                                            write_root=target, actor="upgrade-test")
        new = get_edition(connection, result["editions"][0]["editionId"])
        policy = content_policy(plan["policyVersion"])
        assert new["review_status"] == policy.review_status == "pending-review"
        assert new["content"]["markdown"].encode() == old["content"]["markdown"].encode()
        assert new["content_sha256"] != old["content_sha256"]
        assert new["current_release_id"] == old["current_release_id"]
        assert new["revision_id"] == old["revision_id"]
        # New pending-review events may be appended; old events stay exact and ordered.
        for name in protected:
            rows = [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            assert rows[:len(before[name])] == before[name]
            if name != "publication_edition_reviews":
                assert rows == before[name]
        assert {key: value["kind"] for key, value in plan["entries"][0]["baseline"]["metadataEvidence"].items()} == dict(policy.field_kinds)
    for policy in CONTENT_POLICIES.values():
        assert (Path(__file__).parents[1] / policy.sample).is_file()
    assert content_policy(SOURCE_SUPPLEMENT_POLICY).field_kind("partTitle") == "archive-part-projection"
    assert content_policy(LEGACY_FACTS_POLICY).field_kind("metadataObservedAt") == "unobserved"
    with pytest.raises(ValueError, match="unsupported"):
        content_policy("future-policy")
    with pytest.raises(ValueError, match="unregistered"):
        content_policy(LEGACY_FACTS_POLICY).field_kind("invented")


def test_cli_derives_vtt_without_changing_source_or_requiring_inference(tmp_path, capsys, monkeypatch):
    source = tmp_path / "source"
    frozen_archive(source)
    before = hashlib.sha256((source / "archive.db").read_bytes()).hexdigest()
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("derivation must be offline"))
    output = tmp_path / "derived.vtt"
    assert cli.main(["archive", "derive-vtt", "--source-root", str(source),
                     "--transcript-id", "2", "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.400")
    assert '"asr_calls": 0' in capsys.readouterr().out
    report = export_transcript_vtt(source, transcript_id=2, output=output)
    assert report["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert hashlib.sha256((source / "archive.db").read_bytes()).hexdigest() == before
    output.write_bytes(b"other evidence")
    with pytest.raises(ValueError, match="different bytes"):
        export_transcript_vtt(source, transcript_id=2, output=output)
    assert output.read_bytes() == b"other evidence"


@pytest.mark.parametrize("damage", ["hash", "timeline", "inside", "traversal", "explicit-database"])
def test_invalid_derivative_evidence_never_installs_output(tmp_path, damage):
    from bili_asr.storage.transcripts import _segment_content_sha256
    source = tmp_path / "source"
    frozen_archive(source)
    output = tmp_path / "derived.vtt"
    if damage in {"hash", "timeline"}:
        with closing(connect_database(source / "archive.db")) as connection:
            with connection:
                if damage == "hash":
                    connection.execute("UPDATE transcripts SET content_sha256=? WHERE transcript_id=2", ("0" * 64,))
                else:
                    connection.execute("INSERT INTO transcript_segments VALUES (2,1,0,900,'backward evidence')")
                    connection.execute("UPDATE transcript_segments SET start_ms=1000 WHERE transcript_id=2 AND ordinal=0")
                    rows = connection.execute("SELECT start_ms,end_ms,text FROM transcript_segments WHERE transcript_id=2 ORDER BY ordinal").fetchall()
                    connection.execute("UPDATE transcripts SET content_sha256=? WHERE transcript_id=2",
                                       (_segment_content_sha256([list(row) for row in rows]),))
    elif damage == "explicit-database":
        output = source / "derived.vtt"
    else:
        output = source / "derived.vtt" if damage == "inside" else tmp_path / "unused" / ".." / "source" / "derived.vtt"
    with pytest.raises(ValueError):
        export_transcript_vtt(source / "archive.db" if damage == "explicit-database" else source,
                              transcript_id=2, output=output)
    assert not output.exists()
