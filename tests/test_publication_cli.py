"""CLI behavior for exact-version review, public releases and private exports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.mark.parametrize('link_database', [False, True])
def test_show_rejects_linked_archive_or_database(tmp_path, capsys, link_database):
    from bili_asr.storage import open_database
    from bili_asr.cli import main

    actual = tmp_path / 'actual'
    open_database(actual).close()
    linked = tmp_path / 'linked'
    try:
        if link_database:
            linked.mkdir()
            (linked / 'archive.db').symlink_to(actual / 'archive.db')
        else:
            linked.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'platform cannot create symlinks: {exc}')
    before = (actual / 'archive.db').read_bytes()
    assert main(['publication','show','--archive-root',str(linked),'--edition-id','unused']) == 1
    assert 'link or reparse' in capsys.readouterr().err
    assert (actual / 'archive.db').read_bytes() == before

from bili_asr import cli
from bili_asr.cli.parser import build_parser
from bili_asr.cli.publication import archive_connection
from bili_asr.editorial import EditorialConfig
from bili_asr.storage import open_database
from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
from tests.test_workflow_control_plane import _seed_part


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


@pytest.fixture
def rendered_archive(tmp_path):
    archive = tmp_path / "archive"
    connection = open_database(archive)
    try:
        _seed_part(connection)
        insert_record(connection, record())
        workflow, repository, _, executor = runtime(connection, archive, FakeClient())
        prepared = repository.prepare(1, None, EditorialConfig())
        jobs = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
        assert executor.run().succeeded == 2
        revision = repository.revision_for_job(jobs[0])
    finally:
        connection.close()
    return archive, revision


def invoke(capsys, archive, action, *arguments, actor=True):
    argv = ["publication", action, "--archive-root", str(archive), "--format", "json", *arguments]
    if actor:
        argv += ["--actor", "test-reviewer"]
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def approve(capsys, archive, edition):
    from tests.support.content_permissions import grant_test_permission
    with archive_connection(str(archive), readonly=False) as connection:
        grant_test_permission(connection, edition)
    invoke(capsys, archive, "review", "--edition-id", edition["edition_id"],
           "--content-sha256", edition["content_sha256"], "--expected-status", "pending-review",
           "--status", "in-review", "--note", "test review started")
    invoke(capsys, archive, "review", "--edition-id", edition["edition_id"],
           "--content-sha256", edition["content_sha256"], "--expected-status", "in-review",
           "--status", "approved", "--note", "test fixture approved")


def test_cli_complete_release_lifecycle_and_private_review(rendered_archive, tmp_path, capsys):
    archive, revision = rendered_archive
    output = tmp_path / "public"
    edition_a = invoke(capsys, archive, "create", "--revision-id", revision)
    assert edition_a["review_status"] == "pending-review"
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 0
    approve(capsys, archive, edition_a)
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 0
    release_a = invoke(capsys, archive, "publish", "--edition-id", edition_a["edition_id"])
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 1
    catalog_a = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
    assert catalog_a["articles"][0]["releaseId"] == release_a["release_id"]
    old_body = (output / catalog_a["articles"][0]["file"]).read_bytes()

    markdown = tmp_path / "edit.md"
    markdown.write_text("New test publication body.\n", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"title": "Edited title", "editorNote": "Test editor addition."}), encoding="utf-8")
    edition_b = invoke(capsys, archive, "edit", "--edition-id", edition_a["edition_id"],
                       "--markdown-file", str(markdown), "--metadata-file", str(metadata), "--note", "test edit")
    assert edition_b["content_sha256"] != edition_a["content_sha256"]
    assert edition_b["current_release_id"] == release_a["release_id"]
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 1
    assert (output / catalog_a["articles"][0]["file"]).read_bytes() == old_body

    review_output = tmp_path / "private"
    assert cli.main(["editorial", "export", "--archive-root", str(archive), "--revision-id", revision,
                     "--edition-id", edition_b["edition_id"], "--out", str(review_output), "--format", "json"]) == 0
    capsys.readouterr()
    assert (review_output / "ai-draft.md").is_file()
    assert (review_output / "review.md").is_file()
    assert b"Edited title" in (review_output / "edition.md").read_bytes()
    assert not (output / "review.md").exists()

    approve(capsys, archive, edition_b)
    release_b = invoke(capsys, archive, "publish", "--edition-id", edition_b["edition_id"],
                       "--expected-release-id", release_a["release_id"])
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 1
    entry_b = json.loads((output / "catalog.json").read_text(encoding="utf-8"))["articles"][0]
    assert entry_b["releaseId"] == release_b["release_id"]
    assert b"New test publication body." in (output / entry_b["file"]).read_bytes()
    assert invoke(capsys, archive, "publish", "--edition-id", edition_b["edition_id"])["idempotent"]
    invoke(capsys, archive, "withdraw", "--release-id", release_b["release_id"], "--note", "test withdrawal")
    assert invoke(capsys, archive, "export", "--out", str(output), actor=False)["count"] == 0
    assert list(output.rglob("*.md")) == []
    assert list(archive.rglob("publish.md"))


def test_show_displays_reviewable_content_and_read_only_connection(rendered_archive, capsys):
    archive, revision = rendered_archive
    edition = invoke(capsys, archive, "create", "--revision-id", revision)
    assert cli.main(["publication", "show", "--archive-root", str(archive),
                     "--edition-id", edition["edition_id"]]) == 0
    text = capsys.readouterr().out
    assert edition["content_sha256"] in text
    assert edition["content"]["markdown"].strip() in text
    with archive_connection(str(archive)) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM publication_heads")


@pytest.mark.parametrize("command", ["reading-edit", "reading-review", "reading-export"])
def test_removed_commands_are_not_aliases(command):
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args([command])
    assert raised.value.code == 1


def test_missing_archive_is_not_bootstrapped(tmp_path, capsys):
    archive = tmp_path / "missing"
    assert cli.main(["publication", "show", "--archive-root", str(archive), "--edition-id", "missing"]) == 1
    assert not archive.exists()
    assert "no archive database" in capsys.readouterr().err


def test_legacy_archive_refused_before_publication_root_write(tmp_path, capsys):
    archive = tmp_path / "legacy"
    archive.mkdir()
    path = archive / "archive.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE reading_publications (revision_id TEXT)")
        connection.execute("INSERT INTO reading_publications VALUES ('retain')")
    digest_before = hashlib.sha256(path.read_bytes()).hexdigest()
    root = tmp_path / "products"
    root.mkdir()
    assert cli.main(["publication", "publish", "--archive-root", str(archive),
                     "--artifact-root", str(root), "--edition-id", "missing", "--actor", "test"]) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest_before
    assert list(root.iterdir()) == []
    assert "manuscript-schema-contract" in capsys.readouterr().err


def test_metadata_duplicate_keys_rejected_without_new_edition(rendered_archive, tmp_path, capsys):
    archive, revision = rendered_archive
    edition = invoke(capsys, archive, "create", "--revision-id", revision)
    markdown = tmp_path / "edit.md"
    markdown.write_text("Test body", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"title":"first","title":"second"}', encoding="utf-8")
    assert cli.main(["publication", "edit", "--archive-root", str(archive), "--edition-id", edition["edition_id"],
                     "--markdown-file", str(markdown), "--metadata-file", str(metadata),
                     "--actor", "test", "--note", "duplicate fields"]) == 1
    assert "duplicate metadata key" in capsys.readouterr().err
    with archive_connection(str(archive)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM publication_editions").fetchone()[0] == 1
