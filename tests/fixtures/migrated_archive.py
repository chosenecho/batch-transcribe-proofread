"""Build expensive, checkpointed archives once; copy bytes for each test."""
from __future__ import annotations

import shutil
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.publication import get_edition
from bili_asr.services.archive_migration import check_migrated_archive, migrate_archive
from bili_asr.services.preserved_body_import import (
    apply_preserved_body_import, install_preserved_body_extension, plan_preserved_body_import,
)
from bili_asr.services.source_supplement import install_source_supplement_extension
from tests.fixtures.frozen_migration_archive import frozen_archive


def copy_archive(template: Path, target: Path) -> Path:
    # Never copy an open SQLite archive or a partial WAL. Templates are closed
    # before yielding, and each test owns new files, including its database.
    for suffix in ("-wal", "-shm", "-journal"):
        if template.joinpath("archive.db" + suffix).exists():
            raise ValueError("archive template must be closed and checkpointed")
    shutil.copytree(template, target)
    return target


def build_historical_archive(tmp_path_factory):
    base = tmp_path_factory.mktemp("historical-template")
    source, root = base / "old", base / "archive"
    facts = frozen_archive(source)
    migrate_archive(source, root)
    assert install_preserved_body_extension(root)["installed"]
    assert not install_preserved_body_extension(root)["installed"]
    assert check_migrated_archive(root)["valid"]
    return root, facts["ids"]


def build_preserved_archive(tmp_path_factory, historical_archive_template):
    template, _ = historical_archive_template
    root = copy_archive(template, tmp_path_factory.mktemp("preserved-template") / "archive")
    assert install_source_supplement_extension(root)["installed"]
    assert not install_source_supplement_extension(root)["installed"]
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        selection = [{"kind": "edition", "id": row[0]} for row in connection.execute(
            "SELECT current_edition_id FROM publication_heads ORDER BY video_part_id")]
        plan = plan_preserved_body_import(connection, selectors=selection, artifact_roots=(root,))
        imported = apply_preserved_body_import(
            connection, plan=plan, artifact_roots=(root,), write_root=root, actor="importer")
        editions = [get_edition(connection, item["editionId"]) for item in imported["editions"]]
        with connection:
            for index, edition in enumerate(editions):
                connection.execute("UPDATE video_parts SET title=?,updated_at=123 WHERE video_part_id=?",
                    ("概要" if index == 0 else "如何进行真正的哲学反思", edition["video_part_id"]))
    return root, tuple(edition["edition_id"] for edition in editions)
