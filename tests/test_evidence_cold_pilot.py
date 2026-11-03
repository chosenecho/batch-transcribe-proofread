"""A copy-only pilot preserves every original row and requires exact blob bytes."""
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from scripts.pilot_evidence_cold_archive import FIELDS, pilot, verify
from bili_asr.storage.database import connect_database
from bili_asr.storage.editorial_replay import _stored_request, completed_record
from tests.fixtures.frozen_migration_archive import frozen_archive


def selected(database):
    result = []
    with closing(sqlite3.connect(database)) as connection:
        for table, (key, columns) in FIELDS.items():
            for column in sorted(columns):
                identity = connection.execute(f"SELECT {key} FROM {table} WHERE {column} IS NOT NULL ORDER BY rowid LIMIT 1").fetchone()[0]
                result.append(f"{table}:{identity}:{column}")
    return result


def replays(database):
    with closing(connect_database(database, readonly=True)) as connection:
        result = []
        for row in connection.execute("SELECT * FROM editorial_model_calls WHERE response_json IS NOT NULL AND error_code IS NULL"):
            _, fingerprint = _stored_request(row)
            result.append(completed_record(connection, fingerprint, input_id=row["input_id"], chunk_id=row["chunk_id"]))
        return result


def test_pilot_offline_materialization_and_replay_preserve_frozen_database(tmp_path):
    source, output = tmp_path / "archive", tmp_path / "pilot"
    frozen_archive(source)
    database = source / "archive.db"
    original = database.read_bytes()
    before = replays(database)
    assert before
    report = pilot(database, selected(database), output)
    assert report["valid"] and report["copy_only"] and report["fields_verified"] == 4
    assert report["inline_evidence_unchanged"] and report["paid_calls"] == 0
    assert report["gc_enabled"] is False and report["inline_migration"] is False
    assert database.read_bytes() == original
    assert report["unique_blob_bytes"] > 0 and report["selected_raw_bytes"] > 0
    assert replays(output / "archive.db") == before
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["database_sha256"] == hashlib.sha256((output / "archive.db").read_bytes()).hexdigest()
    assert all(entry["raw_sha256"] != entry["encoded_sha256"] for entry in manifest["fields"])
    source.rename(tmp_path / "offline-original")
    assert verify(output)["valid"]  # Reads only the preserved DB and local blobs.


@pytest.mark.parametrize("fault", ["missing", "corrupt", "database", "codec", "length", "key", "mismatch", "format"])
def test_missing_or_corrupt_cold_evidence_fails_without_replay_or_gc(tmp_path, fault):
    source, output = tmp_path / "archive", tmp_path / "pilot"
    frozen_archive(source)
    database = source / "archive.db"
    original = database.read_bytes()
    pilot(database, selected(database), output)
    path = output / "manifest.json"
    manifest = json.loads(path.read_bytes())
    first = manifest["fields"][0]
    blob = output / "blobs" / (first["encoded_sha256"] + ".zlib")
    if fault == "missing":
        blob.unlink()
    elif fault == "corrupt":
        data = blob.read_bytes()
        blob.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    elif fault == "database":
        (output / "archive.db").write_bytes(b"damaged")
    elif fault == "codec":
        first["codec"] = "unknown"
    elif fault == "length":
        first["raw_bytes"] = 2**40
    elif fault == "key":
        first["encoded_sha256"] = "../escape"
    elif fault == "mismatch":
        first["raw_sha256"] = "f" * 64
    else:
        manifest["copy_only"] = False
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        verify(output)
    assert database.read_bytes() == original


@pytest.mark.parametrize("selection", ["", "unknown:id:field", "editorial_inputs:id:unknown", "editorial_inputs::prepared_json", "editorial_inputs:absent:prepared_json"])
def test_pilot_requires_explicit_existing_fields(tmp_path, selection):
    source = tmp_path / "archive"
    frozen_archive(source)
    with pytest.raises(ValueError):
        pilot(source / "archive.db", [selection], tmp_path / "pilot")
    assert not (tmp_path / "pilot").exists()


def test_pilot_does_not_overwrite_or_publish_into_source(tmp_path):
    source = tmp_path / "archive"
    frozen_archive(source)
    database = source / "archive.db"
    selections = selected(database)
    with pytest.raises(ValueError, match="distinct"):
        pilot(database, [selections[0], selections[0]], tmp_path / "duplicate")
    with pytest.raises(ValueError, match="outside"):
        pilot(database, selections, source / "nested")
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(ValueError, match="new directory"):
        pilot(database, selections, output)
