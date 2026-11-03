"""Version-bound approvals, publication heads and filesystem failure contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import pytest

from bili_asr.editorial import EditorialConfig, digest
from bili_asr import archive as archive_module
from bili_asr.manuscript_files import atomic_write_artifact, secure_path
from bili_asr.publication import (
    PublicationConflictError, create_edition, edit_edition, get_ai_artifacts,
    get_edition, normalize_content, publish_edition, render_publication,
    review_edition, verify_release, withdraw_release,
)
from bili_asr.storage import SchemaContractError, open_database
from bili_asr.storage.database import initialize_schema, require_manuscript_schema
from bili_asr.storage.publication import PublicationRepository
from bili_asr.storage.workflow_outputs import record_bundle_publication
from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
from tests.test_workflow_control_plane import _seed_part


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


def seeded_publication(root: Path):
    """Real new schema plus deterministic offline AI pair for CLI/export tests."""
    connection = open_database(root)
    _seed_part(connection)
    insert_record(connection, record())
    paths = archive_module.write_archive(
        root,
        {"bvid": "BVtest", "cid": 1, "page_index": 0, "work_id": "BVtest:p0",
         "title": "test", "video_title": "test", "pubdate_str": "1970-01-01",
         "duration_s": 1},
        [{"start": 0, "end": 1, "text": "这是一段原始文字"}],
        source="asr",
    )
    connection.execute("BEGIN IMMEDIATE")
    record_bundle_publication(connection, part_id=1, transcript_id=1, paths=paths)
    connection.commit()
    workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    proof_id, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    result = executor.run()
    assert result.succeeded == 2 and result.failed == 0
    return connection, repository.revision_for_job(proof_id), (root,)


def approve(connection, edition: dict, *, roots=None, authorize=True) -> dict:
    from tests.support.content_permissions import grant_test_permission
    if authorize:
        grant_test_permission(connection, edition, roots)
    review_edition(connection, edition_id=edition["edition_id"], status="in-review",
                   content_sha256=edition["content_sha256"], expected_status="pending-review", actor="reviewer")
    return review_edition(connection, edition_id=edition["edition_id"], status="approved",
                          content_sha256=edition["content_sha256"], expected_status="in-review", actor="reviewer")


@pytest.fixture
def archive(tmp_path):
    connection, revision, roots = seeded_publication(tmp_path)
    try:
        yield connection, revision, roots
    finally:
        connection.close()


def _create(archive):
    connection, revision, roots = archive
    return create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")


def _publish(archive, edition, expected=None):
    connection, _, roots = archive
    return publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=roots,
                           write_root=roots[0], actor="publisher", expected_release_id=expected)


def test_a_b_lifecycle_keeps_publication_head_independent_from_drafts(archive):
    connection, _, roots = archive
    a = _create(archive)
    assert a["review_status"] == "pending-review" and a["current_release_id"] is None
    assert "由人工审核发布" not in a["content"]["attribution"]
    with pytest.raises(ValueError, match="not been approved"):
        _publish(archive, a)
    approve(connection, a)
    release_a = _publish(archive, a)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second edition",
                     actor="editor", note="clarify")
    assert b["current_release_id"] == release_a["release_id"]
    review_edition(connection, edition_id=b["edition_id"], status="in-review",
                   content_sha256=b["content_sha256"], expected_status="pending-review", actor="reviewer")
    review_edition(connection, edition_id=b["edition_id"], status="changes-requested",
                   content_sha256=b["content_sha256"], expected_status="in-review", actor="reviewer", note="check source")
    assert verify_release(connection, release_a["release_id"], roots)[0]["status"] == "published"
    review_edition(connection, edition_id=b["edition_id"], status="in-review",
                   content_sha256=b["content_sha256"], expected_status="changes-requested", actor="reviewer")
    review_edition(connection, edition_id=b["edition_id"], status="approved",
                   content_sha256=b["content_sha256"], expected_status="in-review", actor="reviewer")
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release_a["release_id"]
    from tests.support.content_permissions import grant_test_permission
    grant_test_permission(connection, b)
    release_b = _publish(archive, b, release_a["release_id"])
    assert verify_release(connection, release_a["release_id"], roots)[0]["status"] == "superseded"
    assert verify_release(connection, release_b["release_id"], roots)[2] == render_publication(b["content"])
    withdraw_release(connection, release_id=release_b["release_id"], actor="publisher", note="correction required")
    assert get_edition(connection, b["edition_id"])["current_release_id"] is None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("status", ["superseded", "withdrawn"])
def test_direct_release_status_change_without_audit_is_rejected_on_read_and_retry(archive, status):
    connection, _, roots = archive
    edition = _create(archive)
    approve(connection, edition)
    release = _publish(archive, edition)
    with connection:
        connection.execute("UPDATE publication_releases SET status = ? WHERE release_id = ?",
                           (status, release["release_id"]))
        connection.execute("UPDATE publication_heads SET current_release_id = NULL")
    with pytest.raises(ValueError, match="release state disagrees with audit"):
        verify_release(connection, release["release_id"], roots)
    with pytest.raises(ValueError, match="release state disagrees with audit"):
        _publish(archive, edition)


def test_direct_pending_review_detail_change_does_not_silently_enter_review_export(archive):
    connection, _, _ = archive
    edition = _create(archive)
    review_edition(connection, edition_id=edition["edition_id"], status="in-review",
                   content_sha256=edition["content_sha256"], expected_status="pending-review",
                   actor="reviewer", note="real review note", issue_url="https://example.test/issues/258")
    with connection:
        connection.execute("UPDATE publication_edition_reviews SET note = 'changed outside review API'")
    with pytest.raises(ValueError, match="review details disagree with audit"):
        get_edition(connection, edition["edition_id"])


@pytest.mark.parametrize("artifact_name", ["ai-draft.md", "review.md"])
def test_ai_pair_hash_cannot_be_rebound_to_changed_bytes_under_the_same_revision(archive, artifact_name):
    connection, revision, roots = archive
    row = connection.execute("SELECT relative_path FROM document_artifacts WHERE artifact_name = ?",
                             (artifact_name,)).fetchone()
    changed = b"Changed artifact that does not match frozen AI blocks.\n"
    (roots[0] / row["relative_path"]).write_bytes(changed)
    with connection:
        connection.execute("UPDATE document_artifacts SET content_sha256 = ? WHERE artifact_name = ?",
                           (hashlib.sha256(changed).hexdigest(), artifact_name))
    with pytest.raises(ValueError, match="AI artifact identity does not match revision"):
        get_ai_artifacts(connection, revision, roots)
    with pytest.raises(ValueError, match="AI artifact identity does not match revision"):
        _create(archive)
    assert connection.execute("SELECT COUNT(*) FROM publication_editions").fetchone()[0] == 0


def test_reader_fields_and_source_are_frozen_and_entire_object_is_hashed(archive):
    connection, _, _ = archive
    a = _create(archive)
    for key, value in (("title", "new title"), ("markdown", "new text"), ("summary", "intro"),
                       ("tags", ["topic"]), ("attribution", "human expansion"), ("editorNote", "context added")):
        content = deepcopy(a["content"])
        content[key] = value
        assert digest(normalize_content(content)) != a["content_sha256"]
    with connection:
        connection.execute("UPDATE videos SET title = 'changed metadata'")
    assert get_edition(connection, a["edition_id"])["content"]["title"] == "test"
    approve(connection, a)
    release = _publish(archive, a)
    assert b"changed metadata" not in (archive[2][0] / release["relative_path"]).read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        edit_edition(connection, edition_id=a["edition_id"], markdown_text="x",
                     metadata={"source": {}}, actor="editor", note="source")
    with pytest.raises(ValueError, match="attribution must not be empty"):
        edit_edition(connection, edition_id=a["edition_id"], markdown_text="x",
                     metadata={"attribution": ""}, actor="editor", note="remove attribution")


def test_newline_normalization_is_stable_and_empty_invalid_content_is_rejected(archive):
    a = _create(archive)
    content = deepcopy(a["content"])
    content["markdown"] = "paragraph\r\n\r\nnext\r\n"
    content["summary"] = "intro\r\nnext"
    normalized = normalize_content(content)
    assert normalized["markdown"] == "paragraph\n\nnext\n"
    assert normalize_content(normalized) == normalized
    content["tags"] = ["same", "same"]
    with pytest.raises(ValueError, match="duplicate"):
        normalize_content(content)


def test_approval_hash_is_exact_and_review_state_uses_cas(archive):
    connection, _, _ = archive
    a = _create(archive)
    before = connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0]
    with pytest.raises(ValueError, match="does not match"):
        review_edition(connection, edition_id=a["edition_id"], status="in-review",
                       content_sha256="0" * 64, expected_status="pending-review", actor="reviewer")
    assert connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0] == before
    approve(connection, a)
    with pytest.raises(PublicationConflictError, match="expected status"):
        review_edition(connection, edition_id=a["edition_id"], status="rejected",
                       content_sha256=a["content_sha256"], expected_status="in-review", actor="reviewer")
    with pytest.raises(ValueError, match="invalid transition"):
        review_edition(connection, edition_id=a["edition_id"], status="in-review",
                       content_sha256=a["content_sha256"], expected_status="approved", actor="reviewer")
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="new body", actor="editor", note="new content")
    with pytest.raises(ValueError, match="not been approved"):
        _publish(archive, b)


def test_editions_use_parent_cas_without_changing_release_or_events_on_conflict(archive):
    connection, revision, roots = archive
    a = _create(archive)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="new", actor="one", note="first")
    before = [tuple(row) for row in connection.execute("SELECT * FROM publication_events")]
    for call in (
        lambda: edit_edition(connection, edition_id=a["edition_id"], markdown_text="stale", actor="two", note="second"),
        lambda: create_edition(connection, revision_id=revision, artifact_roots=roots, actor="two"),
    ):
        with pytest.raises(PublicationConflictError):
            call()
    assert get_edition(connection, b["edition_id"])["current_edition_id"] == b["edition_id"]
    assert before == [tuple(row) for row in connection.execute("SELECT * FROM publication_events")]


def test_two_connections_cannot_save_from_the_same_stale_parent(archive):
    connection, _, roots = archive
    a = _create(archive)
    second_connection = open_database(roots[0])
    try:
        second_view = get_edition(second_connection, a["edition_id"])
        b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="first editor",
                         actor="first", note="first save")
        with pytest.raises(PublicationConflictError):
            edit_edition(second_connection, edition_id=second_view["edition_id"],
                         markdown_text="second editor", actor="second", note="stale save")
        assert get_edition(second_connection, a["edition_id"])["current_edition_id"] == b["edition_id"]
        assert connection.execute("SELECT COUNT(*) FROM publication_editions").fetchone()[0] == 2
    finally:
        second_connection.close()


def test_new_ai_revision_does_not_replace_an_existing_edition_or_release(archive):
    connection, old_revision, roots = archive
    a = _create(archive)
    approve(connection, a)
    release_a = _publish(archive, a)
    insert_record(connection, replace(record(("new source revision",), transcript_id=2), version=2))
    workflow, repository, _handlers, executor = runtime(connection, roots[0], FakeClient())
    prepared = repository.prepare(2, None, EditorialConfig())
    proof_id, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    new_revision = repository.revision_for_job(proof_id)
    assert new_revision != old_revision
    head = get_edition(connection, a["edition_id"])
    assert head["current_edition_id"] == a["edition_id"]
    assert head["current_release_id"] == release_a["release_id"]
    b = create_edition(connection, revision_id=new_revision, artifact_roots=roots,
                       expected_edition_id=a["edition_id"], actor="editor", note="new source")
    assert b["revision_id"] == new_revision and b["parent_edition_id"] == a["edition_id"]
    assert b["current_release_id"] == release_a["release_id"]
    assert verify_release(connection, release_a["release_id"], roots)[1]["revision_id"] == old_revision


def test_cross_part_heads_and_frozen_source_identity_are_rejected(archive):
    connection, _, roots = archive
    a = _create(archive)
    with connection:
        connection.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,processing_status,created_at,updated_at) "
                           "VALUES (2,'BVtest',1,2,'second',1000,'metadata_collected',1,1)")
    insert_record(connection, record(("second part",), transcript_id=2, part=2))
    workflow, repository, _handlers, executor = runtime(connection, roots[0], FakeClient())
    prepared = repository.prepare(2, None, EditorialConfig())
    proof_id, *_ = workflow.request_editorial(video_part_id=2, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision = repository.revision_for_job(proof_id)
    b = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")
    with connection:
        connection.execute("UPDATE publication_heads SET current_edition_id = ? WHERE video_part_id = 1", (b["edition_id"],))
    with pytest.raises(ValueError, match="cross-part"):
        get_edition(connection, a["edition_id"])
    with connection:
        connection.execute("UPDATE publication_heads SET current_edition_id = ? WHERE video_part_id = 1", (a["edition_id"],))
        connection.execute("UPDATE video_parts SET page_index = 3 WHERE video_part_id = 2")
    with pytest.raises(ValueError, match="another video part"):
        get_edition(connection, b["edition_id"])


def test_review_cannot_be_forged_by_updating_a_pending_state_without_an_event(archive):
    connection, _, _ = archive
    a = _create(archive)
    with connection:
        connection.execute("UPDATE publication_edition_reviews SET status = 'approved' WHERE edition_id = ?",
                           (a["edition_id"],))
    with pytest.raises(ValueError, match="audit event"):
        _publish(archive, a)
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 0


def test_repeat_publish_and_withdraw_are_idempotent_and_history_never_reactivates(archive):
    connection, _, roots = archive
    a = _create(archive)
    approve(connection, a)
    release = _publish(archive, a)
    count = connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0]
    assert _publish(archive, a)["idempotent"] is True
    assert connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0] == count
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second", actor="editor", note="update")
    approve(connection, b)
    release_b = _publish(archive, b, release["release_id"])
    assert _publish(archive, a)["status"] == "superseded"
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release_b["release_id"]
    withdraw_release(connection, release_id=release["release_id"], actor="publisher", note="historical concern")
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release_b["release_id"]
    assert _publish(archive, a)["status"] == "withdrawn"
    count = connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0]
    assert withdraw_release(connection, release_id=release["release_id"], actor="publisher", note="retry")["idempotent"]
    assert connection.execute("SELECT COUNT(*) FROM publication_events").fetchone()[0] == count
    assert verify_release(connection, release["release_id"], roots)[0]["status"] == "withdrawn"


def test_publish_cas_failure_does_not_write_new_release(archive):
    connection, _, _ = archive
    a = _create(archive)
    approve(connection, a)
    release = _publish(archive, a)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second", actor="editor", note="update")
    approve(connection, b)
    with pytest.raises(PublicationConflictError):
        _publish(archive, b)
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release["release_id"]
    assert len(list((archive[2][0] / "publications").rglob("publish.md"))) == 1


def test_database_failure_after_file_write_keeps_old_release_and_retry_reuses_orphan(archive, monkeypatch):
    connection, _, roots = archive
    a = _create(archive)
    approve(connection, a)
    release_a = _publish(archive, a)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second", actor="editor", note="update")
    approve(connection, b)
    original = PublicationRepository.event
    def fail_event(self, edition, event_type, *args, **kwargs):
        if event_type == "published":
            raise RuntimeError("simulated event failure")
        return original(self, edition, event_type, *args, **kwargs)
    monkeypatch.setattr(PublicationRepository, "event", fail_event)
    with pytest.raises(RuntimeError, match="simulated"):
        _publish(archive, b, release_a["release_id"])
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release_a["release_id"]
    assert verify_release(connection, release_a["release_id"], roots)[0]["status"] == "published"
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 1
    paths = list((roots[0] / "publications").rglob("publish.md"))
    assert len(paths) == 2
    before = {p: p.read_bytes() for p in paths}
    monkeypatch.setattr(PublicationRepository, "event", original)
    from tests.support.content_permissions import grant_test_permission
    grant_test_permission(connection, b)
    release_b = _publish(archive, b, release_a["release_id"])
    assert before == {p: p.read_bytes() for p in paths}
    assert _publish(archive, b)["release_id"] == release_b["release_id"]
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 2


def test_database_commit_failure_rolls_back_release_and_preserves_orphan_for_retry(archive, monkeypatch):
    connection, _, roots = archive
    a = _create(archive)
    approve(connection, a)
    # A deferred foreign-key failure happens only at commit, after all service
    # writes and events, and exercises the transaction's actual rollback path.
    connection.execute("CREATE TABLE commit_fault(target TEXT REFERENCES publication_editions(edition_id) DEFERRABLE INITIALLY DEFERRED)")
    original = PublicationRepository.register_release
    def fail_commit(self, **kwargs):
        result = original(self, **kwargs)
        self.connection.execute("INSERT INTO commit_fault VALUES ('missing')")
        return result
    monkeypatch.setattr(PublicationRepository, "register_release", fail_commit)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _publish(archive, a)
    assert get_edition(connection, a["edition_id"])["current_release_id"] is None
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM commit_fault").fetchone()[0] == 0
    paths = list((roots[0] / "publications").rglob("publish.md"))
    assert len(paths) == 1
    monkeypatch.setattr(PublicationRepository, "register_release", original)
    release = _publish(archive, a)
    assert paths[0] == roots[0] / release["relative_path"]
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 1


def test_file_write_failure_preserves_old_database_head_and_retry_succeeds(archive, monkeypatch):
    import bili_asr.publication as service
    connection, _, roots = archive
    a = _create(archive)
    approve(connection, a)
    release_a = _publish(archive, a)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second", actor="editor", note="update")
    approve(connection, b)
    original = service.atomic_write_artifact
    def fail_write(*args, **kwargs):
        raise OSError("simulated fsync failure")
    monkeypatch.setattr(service, "atomic_write_artifact", fail_write)
    with pytest.raises(OSError, match="simulated"):
        _publish(archive, b, release_a["release_id"])
    assert get_edition(connection, b["edition_id"])["current_release_id"] == release_a["release_id"]
    assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 1
    monkeypatch.setattr(service, "atomic_write_artifact", original)
    assert _publish(archive, b, release_a["release_id"])["status"] == "published"


def test_publish_rechecks_head_when_withdrawal_wins_during_file_install(archive, monkeypatch):
    import bili_asr.publication as service
    connection, _, roots = archive
    a = _create(archive)
    approve(connection, a)
    release_a = _publish(archive, a)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="second", actor="editor", note="update")
    approve(connection, b)
    second = open_database(roots[0])
    original = service.atomic_write_artifact
    def concurrent_withdraw(*args, **kwargs):
        target = original(*args, **kwargs)
        withdraw_release(second, release_id=release_a["release_id"], actor="other", note="withdraw before publish")
        return target
    monkeypatch.setattr(service, "atomic_write_artifact", concurrent_withdraw)
    try:
        with pytest.raises(PublicationConflictError):
            _publish(archive, b, release_a["release_id"])
        assert get_edition(connection, b["edition_id"])["current_release_id"] is None
        assert connection.execute("SELECT COUNT(*) FROM publication_releases").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM publication_releases").fetchone()[0] == "withdrawn"
    finally:
        second.close()


@pytest.mark.parametrize("missing", [False, True])
def test_registered_release_corruption_or_absence_never_gets_silently_repaired(archive, missing):
    a = _create(archive)
    approve(archive[0], a)
    release = _publish(archive, a)
    path = archive[2][0] / release["relative_path"]
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="missing|hash mismatch"):
        _publish(archive, a)
    assert (not path.exists()) if missing else path.read_bytes() == b"changed"


def test_ai_pair_path_and_content_integrity_rejected_before_creating_edition(archive):
    connection, revision, roots = archive
    with connection:
        connection.execute("UPDATE document_artifacts SET relative_path = '../escape.md' WHERE artifact_name = 'ai-draft.md'")
    with pytest.raises(ValueError, match="identity"):
        _create(archive)
    assert connection.execute("SELECT COUNT(*) FROM publication_editions").fetchone()[0] == 0


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "C:/absolute", "a//b", "a/./b", "a\\b", "a:stream"])
def test_artifact_paths_reject_noncanonical_or_escaping_names(tmp_path, relative):
    with pytest.raises(ValueError, match="manuscript-path"):
        secure_path(tmp_path, relative, create_parents=True)


def test_artifact_root_and_output_ancestors_reject_symlinks(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    for root, relative in ((linked, "publish.md"), (tmp_path, "linked/publish.md")):
        with pytest.raises(ValueError, match="link or reparse"):
            atomic_write_artifact(root, relative, b"body")
    assert not (actual / "publish.md").exists()


def test_immutable_database_records_reject_direct_mutation(archive):
    connection, _, _ = archive
    a = _create(archive)
    approve(connection, a)
    release = _publish(archive, a)
    statements = [
        ("UPDATE publication_editions SET content_json = '{}' WHERE edition_id = ?", a["edition_id"]),
        ("DELETE FROM publication_editions WHERE edition_id = ?", a["edition_id"]),
        ("UPDATE publication_edition_reviews SET status = 'pending-review' WHERE edition_id = ?", a["edition_id"]),
        ("UPDATE publication_releases SET relative_path = 'wrong' WHERE release_id = ?", release["release_id"]),
        ("DELETE FROM publication_releases WHERE release_id = ?", release["release_id"]),
    ]
    for sql, identity in statements:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(sql, (identity,))
        connection.rollback()


def test_old_schema_rejected_before_any_database_writes(tmp_path):
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as legacy:
        legacy.executescript("CREATE TABLE reading_document_editions(edition_id TEXT PRIMARY KEY);"
                             "INSERT INTO reading_document_editions VALUES ('legacy');")
    before = path.read_bytes()
    with pytest.raises(SchemaContractError, match="manuscript-schema-contract"):
        open_database(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as legacy:
        assert legacy.execute("SELECT * FROM reading_document_editions").fetchall() == [("legacy",)]
        assert legacy.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [("reading_document_editions",)]


def test_missing_or_altered_contract_is_not_repaired_on_open(tmp_path):
    path = tmp_path / "archive.db"
    connection = open_database(path)
    connection.execute("DROP TRIGGER immutable_publication_editions_update")
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SchemaContractError):
        open_database(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_metadata_only_archive_does_not_gain_a_manuscript_contract(tmp_path):
    from importlib import resources

    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as connection:
        package = resources.files("bili_asr.storage")
        for name in ("schema.sql", "schema-transcripts.sql", "schema-workflow.sql"):
            connection.executescript(package.joinpath(name).read_text("utf-8"))
    connection = open_database(path)
    try:
        with pytest.raises(SchemaContractError):
            require_manuscript_schema(connection)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'publication_editions'").fetchone()[0] == 0
    finally:
        connection.close()


def test_readonly_connection_checks_contract_without_writing(archive):
    connection, revision, roots = archive
    connection.close()
    readonly = sqlite3.connect(f"file:{(roots[0] / 'archive.db').as_posix()}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA foreign_keys = ON")
    try:
        require_manuscript_schema(readonly)
        assert set(get_ai_artifacts(readonly, revision, roots)) == {"ai-draft.md", "review.md"}
    finally:
        readonly.close()


def test_historical_templates_ignore_current_writer_changes(archive, monkeypatch):
    import bili_asr.publication as publication
    import bili_asr.editorial as editorial
    connection, revision, roots = archive
    edition = _create(archive)
    approve(connection, edition)
    release = _publish(archive, edition)
    original = verify_release(connection, release["release_id"], roots)[2]
    original_pair = get_ai_artifacts(connection, revision, roots)
    monkeypatch.setattr(publication, "PUBLISH_TEMPLATE_VERSION", "publish-v2")
    monkeypatch.setattr(editorial, "TEMPLATE_VERSION", "ai-draft-v2")
    monkeypatch.setattr(publication, "render_publication", lambda content: b"new writer bytes")
    monkeypatch.setattr(editorial, "render_documents", lambda *args: {})
    assert verify_release(connection, release["release_id"], roots)[2] == original
    assert get_ai_artifacts(connection, revision, roots) == original_pair
    assert _publish(archive, edition)["idempotent"]


def test_missing_historical_renderer_is_reported_explicitly(archive, monkeypatch):
    from bili_asr.manuscript_templates import PUBLISH_RENDERERS
    connection, _, roots = archive
    edition = _create(archive)
    approve(connection, edition)
    release = _publish(archive, edition)
    monkeypatch.delitem(PUBLISH_RENDERERS, "publish-v1")
    with pytest.raises(ValueError, match="unsupported template renderer: publish-v1"):
        verify_release(connection, release["release_id"], roots)
