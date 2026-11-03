"""Import strategies preserve history and make interruption observable."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from bili_asr.services import maintenance_import as imports
from bili_asr.services import maintenance_package as package
from bili_asr.storage.upgrade_evidence import readonly
from tests.fixtures.frozen_migration_archive import frozen_archive


def exported(tmp_path, part):
    source = tmp_path / "source"
    if not source.exists():
        frozen_archive(source)
    output = tmp_path / f"part-{part}.zip"
    package.export_package(source, output, part_ids=[part])
    return output


def installed(tmp_path, part=1):
    incoming = exported(tmp_path, part)
    target = tmp_path / "target"
    plan = package.plan_import(incoming, target, resumable=True)
    saved = tmp_path / "first-plan.json"
    package.write_plan(plan, saved)
    package.apply_import(saved)
    return target


def test_merge_preview_is_readonly_and_unions_frozen_histories(tmp_path):
    target = installed(tmp_path)
    before = (target / "archive.db").read_bytes()
    incoming = exported(tmp_path, 2)
    plan = package.plan_import(incoming, target, strategy="merge")
    assert (target / "archive.db").read_bytes() == before
    assert plan["changes"]["overwritten"] == 0
    saved = tmp_path / "merge.json"
    package.write_plan(plan, saved)
    report = package.apply_import(saved)
    assert report["strategy"] == "merge"
    assert package.apply_import(saved)["reused"]
    assert (Path(report["rollback_root"]) / "archive.db").read_bytes() == before
    with closing(readonly(target / "archive.db")) as connection:
        assert list(connection.execute("SELECT video_part_id FROM video_parts ORDER BY video_part_id")) == [(1,), (2,)]
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert {row[0] for row in connection.execute("SELECT status FROM publication_releases")} == {"published", "superseded", "withdrawn"}
    assert package.check_import(target, plan_id=plan["plan_id"])["valid"]


def test_replace_preserves_old_archive_in_atomic_exchange_and_rejects_drift(tmp_path):
    target = installed(tmp_path)
    before = (target / "archive.db").read_bytes()
    incoming = exported(tmp_path, 2)
    plan = package.plan_import(incoming, target, strategy="replace")
    saved = tmp_path / "replace.json"
    package.write_plan(plan, saved)
    report = package.apply_import(saved)
    assert (Path(report["rollback_root"]) / "archive.db").read_bytes() == before
    with closing(readonly(target / "archive.db")) as connection:
        assert list(connection.execute("SELECT video_part_id FROM video_parts")) == [(2,)]
    (target / "documents" / "changed.json").write_text("{}")
    with pytest.raises(ValueError, match="file set changed"):
        package.apply_import(saved)


def test_merge_conflicting_artifact_fails_before_touching_target(tmp_path):
    target = installed(tmp_path)
    incoming = tmp_path / "conflict.zip"
    source = tmp_path / "source"
    path = source / "documents" / "part-1" / "conflict.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"source")
    other = target / "documents" / "part-1" / "conflict.json"
    other.write_bytes(b"target")
    package.export_package(source, incoming, part_ids=[1])
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="staging file changed"):
        package.plan_import(incoming, target, strategy="merge")
    assert (target / "archive.db").read_bytes() == before


def test_interrupted_final_copy_resumes_completed_files_and_preserves_target(tmp_path, monkeypatch):
    incoming = exported(tmp_path, 1)
    target = tmp_path / "target"
    plan = package.plan_import(incoming, target, resumable=True)
    saved = tmp_path / "resume.json"
    package.write_plan(plan, saved)
    original = imports._copy
    calls = []
    work = target.parent / ("." + target.name + ".maintenance-" + plan["plan_id"])

    def interrupt(source, output, entry):
        if output.is_relative_to(work / "archive"):
            calls.append(entry["path"])
            if len(calls) == 2:
                raise OSError("simulated power loss")
        return original(source, output, entry)

    monkeypatch.setattr(imports, "_copy", interrupt)
    with pytest.raises(OSError, match="power loss"):
        package.apply_import(saved)
    assert not (target / "archive.db").exists()
    assert (work / "checkpoint.json").is_file()
    assert (work / "archive" / "archive.db").is_file()
    monkeypatch.setattr(imports, "_copy", original)
    assert package.apply_import(saved)["valid"]
    assert package.apply_import(saved)["reused"]


def test_target_change_since_plan_and_tampered_checkpoint_fail(tmp_path):
    target = installed(tmp_path)
    plan = package.plan_import(exported(tmp_path, 2), target, strategy="merge")
    saved = tmp_path / "drift.json"
    package.write_plan(plan, saved)
    (target / "documents" / "new.json").write_bytes(b"new fact")
    with pytest.raises(ValueError, match="target changed"):
        package.apply_import(saved)


def test_independent_frozen_editorial_transcript_identity_conflict_is_not_rewritten(tmp_path):
    target = installed(tmp_path)
    source = tmp_path / "source"
    # This frozen fixture contains prepared inputs, model request/response,
    # revisions and publication versions. Change the transcript authority while
    # retaining its colliding identity, as two independent archives can do.
    with sqlite3.connect(source / "archive.db") as connection:
        identity = connection.execute("SELECT transcript_id FROM transcripts WHERE video_part_id=1 LIMIT 1").fetchone()[0]
        connection.execute("UPDATE transcripts SET created_at=created_at+1 WHERE transcript_id=?", (identity,))
    incoming = tmp_path / "independent.zip"
    package.export_package(source, incoming, part_ids=[1])
    before = (target / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="frozen-history identity conflict"):
        package.plan_import(incoming, target, strategy="merge")
    assert (target / "archive.db").read_bytes() == before


def test_external_artifact_root_is_materialized_and_relative_paths_preserved(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    product = tmp_path / "artifacts"
    product.mkdir()
    (source / "audio").rename(product / "audio")
    exported_path = tmp_path / "external.zip"
    package.export_package(source, exported_path, artifact_root=product)
    assert package.verify_package(exported_path)["valid"]


def test_hardlinked_checkpoint_and_partial_copy_cannot_write_external_inode(tmp_path):
    external = tmp_path / "outside.json"
    external.write_bytes(b"outside must remain unchanged")
    pending = tmp_path / "checkpoint.json.pending"
    os.link(external, pending)
    with pytest.raises(ValueError, match="hard-linked"):
        imports._save(tmp_path / "checkpoint.json", {"format": "test"})
    assert external.read_bytes() == b"outside must remain unchanged"
    pending.unlink()
    source = tmp_path / "source.bin"
    source.write_bytes(b"incoming")
    partial = tmp_path / "copy.bin.maintenance-partial"
    os.link(external, partial)
    with pytest.raises(ValueError, match="hard-linked"):
        imports._copy(source, tmp_path / "copy.bin", package._record("copy.bin", source))
    assert external.read_bytes() == b"outside must remain unchanged"
