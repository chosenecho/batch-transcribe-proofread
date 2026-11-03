"""Portable snapshots preserve the publication contract and release history."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from bili_asr.archive_maintenance import archive_access
from bili_asr.publication import (
    create_edition, edit_edition, get_ai_artifacts, get_edition, publish_edition,
    verify_release, withdraw_release,
)
from bili_asr.publication_export import export_publications
from bili_asr.services.archive_snapshot import SnapshotError, check_snapshot, restore_snapshot, save_snapshot
from bili_asr.storage import open_database
from bili_asr.storage.snapshots import required_artifacts, validate_snapshot_database, SnapshotDatabaseError
from tests.test_archive_snapshot import _rewrite_snapshot
from tests.test_publication import approve, seeded_publication


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


def _published_archive(root):
    connection, revision, roots = seeded_publication(root)
    edition = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")
    approve(connection, edition)
    release = publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=roots,
                              write_root=root, actor="publisher")
    return connection, revision, edition, release


@pytest.mark.parametrize("state", ["published", "superseded", "withdrawn"])
def test_snapshot_roundtrip_preserves_frozen_editions_ai_pair_and_all_release_states(tmp_path, state):
    root, products = tmp_path / "source", tmp_path / "products"
    connection, revision, a, release_a = _published_archive(root)
    try:
        b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="Edition B body.\n",
                         metadata={"title": "Frozen B title"}, actor="editor", note="new version")
        releases = [release_a]
        if state == "superseded":
            approve(connection, b)
            releases.append(publish_edition(connection, edition_id=b["edition_id"], artifact_roots=(root,),
                                            write_root=root, actor="publisher",
                                            expected_release_id=release_a["release_id"]))
        elif state == "withdrawn":
            withdraw_release(connection, release_id=release_a["release_id"], actor="publisher", note="withdraw")
        with connection:
            connection.execute("UPDATE videos SET title = 'Mutable live title'")
        expected = {release["release_id"]: verify_release(connection, release["release_id"], (root,))[2]
                    for release in releases}
        products.mkdir()
        for directory in ("documents", "publications"):
            shutil.move(str(root / directory), products / directory)
        before = (root / "archive.db").read_bytes()
        snapshot = tmp_path / "backup.zip"
        saved = save_snapshot(root, snapshot, artifact_root=products)
        assert check_snapshot(snapshot)["snapshot_id"] == saved["snapshot_id"]
        with zipfile.ZipFile(snapshot) as archive:
            assert all(release["relative_path"] in archive.namelist() for release in releases)
            assert not any(name.endswith("reading.md") for name in archive.namelist())
        assert (root / "archive.db").read_bytes() == before
    finally:
        connection.close()
    target = tmp_path / "restored"
    restore_snapshot(snapshot, target)
    restored = open_database(target)
    try:
        assert set(get_ai_artifacts(restored, revision, (target,))) == {"ai-draft.md", "review.md"}
        assert get_edition(restored, b["edition_id"])["content"]["title"] == "Frozen B title"
        for release_id, data in expected.items():
            assert verify_release(restored, release_id, (target,))[2] == data
        assert verify_release(restored, release_a["release_id"], (target,))[0]["status"] == state
        output = tmp_path / "public"
        assert export_publications(restored, artifact_roots=(target,), output=output) == (state != "withdrawn")
        catalog = json.loads((output / "catalog.json").read_text("utf-8"))
        if state != "withdrawn":
            assert catalog["articles"][0]["releaseId"] == releases[-1]["release_id"]
    finally:
        restored.close()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_snapshot_refuses_missing_or_corrupt_historical_release(tmp_path, damage):
    root = tmp_path / "source"
    connection, _, _, release = _published_archive(root)
    withdraw_release(connection, release_id=release["release_id"], actor="publisher", note="withdraw")
    connection.close()
    artifact = root / release["relative_path"]
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"changed historical release")
    output = tmp_path / "backup.zip"
    before = (root / "archive.db").read_bytes()
    with pytest.raises(SnapshotError, match="missing artifact|hash mismatch"):
        save_snapshot(root, output)
    assert not output.exists()
    assert (root / "archive.db").read_bytes() == before


def test_snapshot_check_and_restore_validate_release_hash_against_database(tmp_path):
    root = tmp_path / "source"
    connection, _, _, release = _published_archive(root)
    connection.close()
    snapshot, corrupt = tmp_path / "backup.zip", tmp_path / "changed.zip"
    save_snapshot(root, snapshot)
    replacement = b"different publish file with matching manifest checksum"

    def manifest_change(manifest):
        item = next(item for item in manifest["files"] if item["path"] == release["relative_path"])
        item.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())

    _rewrite_snapshot(snapshot, corrupt, manifest_change,
                      lambda files: files.update({release["relative_path"]: replacement}))
    target = tmp_path / "target"
    target.mkdir()
    for operation in (lambda: check_snapshot(corrupt), lambda: restore_snapshot(corrupt, target)):
        with pytest.raises(SnapshotError, match="database artifact hash mismatch"):
            operation()
    assert list(target.iterdir()) == []


def test_snapshot_refuses_release_state_without_matching_head(tmp_path):
    root = tmp_path / "source"
    connection, _, _, release = _published_archive(root)
    with connection:
        connection.execute("UPDATE publication_heads SET current_release_id = NULL")
    connection.close()
    with pytest.raises(SnapshotError, match="release state disagrees with effective head"):
        save_snapshot(root, tmp_path / "backup.zip")


def test_snapshot_requires_manuscript_marker_value_and_refuses_unfinished_manuscript_file(tmp_path):
    root = tmp_path / "source"
    connection, _, _, _ = _published_archive(root)
    connection.close()
    temporary = root / "publications" / ".manuscript-crashed"
    temporary.write_bytes(b"incomplete")
    with pytest.raises(SnapshotError, match="unfinished temporary artifact"):
        save_snapshot(root, tmp_path / "backup.zip")
    temporary.unlink()
    with sqlite3.connect(root / "archive.db") as raw:
        raw.execute("DELETE FROM manuscript_contract")
    before = (root / "archive.db").read_bytes()
    with pytest.raises(SnapshotDatabaseError, match="manuscript-schema-contract"):
        validate_snapshot_database(root / "archive.db")
    assert (root / "archive.db").read_bytes() == before


@pytest.mark.parametrize("arguments", [
    ["create", "--revision-id", "revision"],
    ["edit", "--edition-id", "edition", "--markdown-file", "body.md", "--note", "edit"],
    ["review", "--edition-id", "edition", "--status", "in-review", "--content-sha256", "a" * 64,
     "--expected-status", "pending-review", "--note", "review"],
    ["publish", "--edition-id", "edition"],
    ["withdraw", "--release-id", "release", "--note", "withdraw"],
])
def test_snapshot_exclusive_lock_blocks_every_cli_publication_write(tmp_path, arguments):
    root = tmp_path / "source"
    connection = open_database(root)
    connection.close()
    before = (root / "archive.db").read_bytes()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    with archive_access(root, exclusive=True):
        result = subprocess.run(
            [sys.executable, "-m", "bili_asr", "publication", *arguments,
             "--actor", "editor", "--archive-root", str(root)],
            capture_output=True, text=True, env=environment, timeout=30,
        )
    assert result.returncode == 1
    assert "archive_busy" in result.stderr
    assert (root / "archive.db").read_bytes() == before
    assert not (root / "publications").exists()
