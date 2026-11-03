from contextlib import closing
import sqlite3

import pytest

from tests.fixtures.migrated_archive import copy_archive


def test_copied_archive_keeps_database_and_artifacts_private(historical_archive_template, tmp_path):
    template, _ = historical_archive_template
    first = copy_archive(template, tmp_path / "first")
    second = copy_archive(template, tmp_path / "second")
    original = (template / "archive.db").read_bytes()
    with closing(sqlite3.connect(first / "archive.db")) as connection, connection:
        connection.execute("UPDATE videos SET title='changed by one test'")
    assert (first / "archive.db").read_bytes() != original
    assert (second / "archive.db").read_bytes() == original
    assert (template / "archive.db").read_bytes() == original
    artifact = next(path for path in (first / "documents").rglob("*") if path.is_file())
    relative = artifact.relative_to(first)
    before = (template / relative).read_bytes()
    artifact.write_bytes(b"changed by one test")
    assert (second / relative).read_bytes() == before
    assert (template / relative).read_bytes() == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_open_archive_template_is_not_copied(tmp_path, suffix):
    template = tmp_path / "template"
    template.mkdir()
    (template / ("archive.db" + suffix)).touch()
    target = tmp_path / "copy"
    with pytest.raises(ValueError, match="closed and checkpointed"):
        copy_archive(template, target)
    assert not target.exists()
