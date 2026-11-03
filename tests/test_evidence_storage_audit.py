import sqlite3

import pytest

from scripts.audit_evidence_storage import audit


def test_utf8_sizes_compression_and_duplicates_are_measured_without_payloads_or_writes(tmp_path):
    database = tmp_path / "audit.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE editorial_inputs(prepared_json TEXT)")
        connection.executemany("INSERT INTO editorial_inputs VALUES (?)", [("中文字" * 100,)] * 2 + [(None,)])
    before = database.read_bytes()
    result = audit(database)
    field = result["fields"][0]
    assert (field["non_null_rows"], field["text_characters"], field["utf8_bytes"]) == (2, 600, 1800)
    assert field["sample"]["duplicate_rows"] == 1
    assert field["sample"]["zlib_bytes"] < field["sample"]["raw_bytes"]
    assert database.read_bytes() == before
    assert "中文字" not in str(result)
    assert result["external_storage_bytes"] is None


def test_wal_commits_are_included_and_oversized_samples_are_not_read(tmp_path):
    database = tmp_path / "audit.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE editorial_inputs(prepared_json TEXT)")
        connection.execute("INSERT INTO editorial_inputs VALUES (?)", ("abc" * 1000,))
        connection.commit()
        report = audit(database, max_sample_bytes=16)
        assert report["fields"][0]["utf8_bytes"] == 3000
        assert report["fields"][0]["sample"]["rows"] == 0
        assert report["database"]["wal_file_bytes"] > 0


@pytest.mark.parametrize("rows", [0, True, 1001])
def test_invalid_sample_limits_fail(rows, tmp_path):
    with pytest.raises(ValueError):
        audit(tmp_path / "absent.db", sample_rows=rows)
