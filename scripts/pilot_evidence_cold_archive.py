"""Copy-only, reversible cold JSON pilot on an independently backed-up database.

No inline field is replaced and no garbage collection or model call exists in
this command. Keep the original database and use a new output directory.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zlib

from bili_asr.artifact_inventory import require_no_links, require_regular_file, stream_hash
from bili_asr.canonical_json import canonical
from bili_asr.storage.snapshots import validate_snapshot_database
from bili_asr.storage_targets import sync_directory, unique_json_object

FIELDS = {"editorial_inputs": ("input_id", {"prepared_json"}),
          "editorial_model_calls": ("call_id", {"request_json", "response_json"}),
          "editorial_revisions": ("revision_id", {"blocks_json"})}
FORMAT = "evidence-cold-pilot/v1"
MAX_FIELD_BYTES = 16 * 1024**2


def _select(value):
    table, separator, rest = value.partition(":")
    identity, separator2, column = rest.rpartition(":")
    if (not separator or not separator2 or not identity or len(identity) > 512
            or table not in FIELDS or column not in FIELDS[table][1]):
        raise ValueError("select requires an allowed TABLE:STABLE_ID:COLUMN")
    return table, identity, column


def _raw(connection, table, identity, column):
    key = FIELDS[table][0]
    row = connection.execute(f"SELECT typeof({column}),length(CAST({column} AS BLOB)) FROM {table} WHERE {key}=?", (identity,)).fetchone()
    if row is None or row[0] != "text" or row[1] > MAX_FIELD_BYTES:
        raise ValueError("selected evidence is absent, non-text or exceeds the 16 MiB pilot bound")
    return connection.execute(f"SELECT CAST({column} AS BLOB) FROM {table} WHERE {key}=?", (identity,)).fetchone()[0]


def _file_hash(path):
    with path.open("rb") as stream:
        return stream_hash(stream)[1]


def _materialize(path, entry):
    require_regular_file(path)
    if path.stat().st_size != entry["encoded_bytes"] or entry["encoded_bytes"] > MAX_FIELD_BYTES + 65536:
        raise ValueError("cold blob size differs from receipt")
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != entry["encoded_sha256"]:
        raise ValueError("cold blob digest differs from receipt")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(encoded, entry["raw_bytes"] + 1)
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or len(raw) != entry["raw_bytes"] or hashlib.sha256(raw).hexdigest() != entry["raw_sha256"]):
        raise ValueError("cold blob original bytes are unavailable or corrupt")
    return raw


def verify(directory: Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    require_no_links(directory)
    manifest_path, database = directory / "manifest.json", directory / "archive.db"
    if require_regular_file(manifest_path).st_size > 1024**2:
        raise ValueError("pilot manifest exceeds bound")
    manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=unique_json_object)
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("copy_only") is not True or not isinstance(manifest.get("fields"), list)
            or not 1 <= len(manifest["fields"]) <= 64):
        raise ValueError("invalid cold pilot manifest")
    require_regular_file(database)
    if _file_hash(database) != manifest["database_sha256"]:
        raise ValueError("pilot database differs from the preserved authority")
    if validate_snapshot_database(database) != manifest["database_contract"]:
        raise ValueError("pilot database contract differs")
    latencies = []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        for entry in manifest["fields"]:
            table, identity, column = _select(entry["selection"])
            expected = _raw(connection, table, identity, column)
            if type(entry["raw_bytes"]) is not int or not 0 <= entry["raw_bytes"] <= MAX_FIELD_BYTES:
                raise ValueError("invalid cold field byte bound")
            digest = entry["encoded_sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid cold blob key")
            if entry["codec"] != "zlib/6":
                raise ValueError("unsupported cold blob codec")
            start = time.perf_counter()
            raw = _materialize(directory / "blobs" / (digest + ".zlib"), entry)
            latencies.append(time.perf_counter() - start)
            if raw != expected:
                raise ValueError("materialized JSON differs from original frozen field")
    return {"format": FORMAT, "valid": True, "fields_verified": len(latencies),
            "materialize_seconds": latencies, "paid_calls": 0, "gc_enabled": False,
            "inline_evidence_unchanged": True, "replay_execution_performed": False}


def pilot(database: Path, selections: list[str], output: Path) -> dict:
    selected = [_select(value) for value in selections]
    if not 1 <= len(selected) <= 64 or len(set(selected)) != len(selected):
        raise ValueError("select 1..64 distinct evidence fields explicitly")
    database = Path(database).resolve(strict=True)
    require_regular_file(database)
    output = Path(os.path.abspath(output))
    require_no_links(output)
    if output.exists() or not output.parent.is_dir() or output.is_relative_to(database.parent):
        raise ValueError("pilot output requires a new directory outside the source archive")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=".cold-evidence-", dir=output.parent) as temporary:
        stage = Path(temporary) / "pilot"
        stage.mkdir()
        preserved = stage / "archive.db"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(preserved)) as target:
            source.execute("PRAGMA query_only=ON")
            source.backup(target)
        contract = validate_snapshot_database(preserved)
        (stage / "blobs").mkdir()
        entries = []
        with closing(sqlite3.connect(preserved.as_uri() + "?mode=ro", uri=True)) as connection:
            for selection, (table, identity, column) in zip(selections, selected, strict=True):
                raw = _raw(connection, table, identity, column)
                encoded = zlib.compress(raw, 6)
                raw_hash, encoded_hash = hashlib.sha256(raw).hexdigest(), hashlib.sha256(encoded).hexdigest()
                blob = stage / "blobs" / (encoded_hash + ".zlib")
                if not blob.exists():
                    with blob.open("xb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                entries.append({"selection": selection, "raw_bytes": len(raw), "raw_sha256": raw_hash,
                                "encoded_bytes": len(encoded), "encoded_sha256": encoded_hash, "codec": "zlib/6"})
        manifest = {"format": FORMAT, "copy_only": True, "database_contract": contract,
                    "database_sha256": _file_hash(preserved), "fields": entries,
                    "gc_enabled": False, "inline_migration": False}
        with (stage / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        checked = verify(stage)
        with preserved.open("r+b") as stream:
            os.fsync(stream.fileno())
        sync_directory(stage / "blobs")
        sync_directory(stage)
        os.rename(stage, output)
        sync_directory(output.parent)
    return {**checked, "copy_only": True, "inline_migration": False,
            "database_bytes": (output / "archive.db").stat().st_size,
            "selected_raw_bytes": sum(entry["raw_bytes"] for entry in entries),
            "unique_blob_bytes": sum(path.stat().st_size for path in (output / "blobs").iterdir()),
            "pilot_total_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
            "peak_memory_measured": False,
            "elapsed_seconds": time.perf_counter() - started, "rollback": "discard-independent-pilot; original archive unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--select", action="append", required=True)
    create.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = pilot(args.database, args.select, args.output) if args.action == "create" else verify(args.directory)
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, zlib.error):
        parser.exit(1, "cold pilot failed: evidence_unavailable_or_invalid; no inference or GC attempted\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
