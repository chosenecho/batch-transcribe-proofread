"""Issue #515: read-only storage composition evidence for an archive database."""

from __future__ import annotations

import json
import os

import pytest

from bili_asr import cli
from bili_asr.services.storage_report import SCHEMA, storage_report
from tests.test_ai_editorial import database  # noqa: F401

BIG = json.dumps({"text": "重复内容" * 4000, "segments": list(range(200))}, ensure_ascii=False)


def add_inputs(connection, count: int, *, created_at: int = 1_700_000_000) -> bytes:
    payload = BIG.encode("utf-8")
    with connection:
        for index in range(count):
            connection.execute(
                "INSERT INTO editorial_inputs(input_id, video_part_id, base_transcript_id, "
                "reference_transcript_id, prepared_json, created_at) VALUES (?,?,?,?,?,?)",
                (f"report-input-{index}", 1, 1, None, BIG, created_at + index))
    return payload


def test_report_separates_bytes_characters_pages_and_exact_duplicates(database):
    payload = add_inputs(database, 2)
    report = storage_report(database, sample_rows=2, top_tables=10, top_columns=10)
    assert report["schema"] == SCHEMA
    assert report["page_object_bytes_available"] is True
    assert report["contracts"]["manuscript_contract"] == 1
    assert report["database"]["page_size"] > 0
    assert report["database"]["page_count"] > 0
    assert report["database"]["journal_mode"] == "delete"
    assert report["database"]["file_bytes"] > 0
    tables = {table["name"]: table for table in report["tables"]}
    inputs = tables["editorial_inputs"]
    assert inputs["rows"] == 2
    assert inputs["page_bytes"] and inputs["page_bytes"] > 0
    columns = {column["name"]: column for column in inputs["columns"]}
    prepared = columns["prepared_json"]
    assert prepared["logical_bytes"] == 2 * len(payload)
    assert prepared["characters"] == 2 * len(BIG)
    assert prepared["logical_bytes"] != prepared["characters"]
    growth = {item["table"]: item for item in report["growth"]}
    assert growth["editorial_inputs"]["time_column"] == "created_at"
    assert growth["editorial_inputs"]["months"] == [
        {"month": "2023-11", "rows": 2, "logical_bytes": inputs["logical_bytes"]}]
    measured = {(item["table"], item["column"]): item for item in report["large_columns"]}
    column = measured[("editorial_inputs", "prepared_json")]
    assert column["rows"] == 2
    assert column["logical_bytes"] == 2 * len(payload)
    assert column["distinct_contents"] == 1
    assert column["duplicate_bytes"] == len(payload)
    assert column["duplicate_ratio"] == pytest.approx(0.5)
    sample = column["compression_sample"]
    assert 1 <= sample["rows"] <= 2
    assert 0 < sample["compressed_bytes"] < sample["raw_bytes"]
    assert sample["projected_compressed_bytes"] < column["logical_bytes"]


def test_report_does_not_modify_the_database(database):
    add_inputs(database, 2)
    path = database.execute("PRAGMA database_list").fetchone()[2]
    before = os.path.getsize(path)
    storage_report(database, sample_rows=1, top_tables=5, top_columns=5)
    assert os.path.getsize(path) == before
    assert not os.path.exists(path + "-wal")
    assert not os.path.exists(path + "-journal")


@pytest.mark.parametrize("options", [
    {"sample_rows": 0}, {"sample_rows": 65}, {"top_tables": 0}, {"top_tables": 101},
    {"top_columns": 0}, {"top_columns": True},
])
def test_report_rejects_unbounded_options(database, options):
    with pytest.raises(ValueError):
        storage_report(database, **options)


def test_cli_storage_report_reads_an_archive_root(database, capsys, tmp_path):
    add_inputs(database, 1)
    assert cli.main(["archive", "storage-report", "--archive-root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == SCHEMA
    assert report["database"]["path"].endswith("archive.db")
    assert {table["name"] for table in report["tables"]} >= {"editorial_inputs", "workflow_jobs"}
