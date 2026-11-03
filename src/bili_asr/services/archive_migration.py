"""Offline conversion into an independently validated universal archive.

No source DDL, checkpoint, job execution, file re-encoding or in-place upgrade
is performed. Only invocation-owned staging is cleaned on failure. Source and
target locks stay held until the complete target is durably installed.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile

from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.artifact_inventory import require_no_links, require_regular_file, portable_artifact_parts, stream_hash
from bili_asr.canonical_json import canonical
from bili_asr.services.migration_preflight import migration_preflight
from bili_asr.storage.archive_contracts import bootstrap_contract, UNIVERSAL_V2, register_frozen_version
from bili_asr.storage.database import connect_database
from bili_asr.storage.migration_source import legacy_source_contract, inspect_migration_source, iter_legacy_rows, table_fingerprint, typed_row_digest
from bili_asr.storage.snapshots import validate_snapshot_database, required_artifacts


class ArchiveMigrationError(ValueError):
    """Conversion failed without publishing or modifying the source."""


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _empty_target(path: Path) -> None:
    require_no_links(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ArchiveMigrationError("migration target must be nonexistent or empty")


def _backup_legacy(source: Path, target: Path) -> None:
    require_regular_file(source)
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 30:
            raise ArchiveMigrationError("legacy backup exceeded contention deadline")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
        original.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as backup:
            original.backup(backup, pages=256, progress=progress, sleep=.05)
    inspect_migration_source(target)


def _copy_authority(source: Path, target: Path) -> list[dict]:
    contract = legacy_source_contract()
    tables = sorted(name for name, shape in contract["objects"].items() if shape["kind"] == "table")
    first = ["bilibili_users", "videos", "video_parts"]
    tables = first + [name for name in tables if name not in first]
    preserved = []
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as original, closing(connect_database(target)) as converted:
        bootstrap_contract(converted)
        converted.execute("BEGIN IMMEDIATE")
        converted.execute("PRAGMA defer_foreign_keys=ON")
        try:
            for name in tables:
                columns = contract["objects"][name]["columns"]
                if name == "manuscript_contract":
                    converted.execute("DELETE FROM manuscript_contract")
                for rowid, cells in iter_legacy_rows(original, name):
                    placeholders = ["?"] + ["CAST(? AS TEXT)" if kind == "text" else "?" for kind, value in cells]
                    names = ["rowid", *columns]
                    quoted = ",".join('"' + value.replace('"', '""') + '"' for value in names)
                    converted.execute(f'INSERT INTO "{name}"({quoted}) VALUES ({",".join(placeholders)})',
                                      (rowid, *(value for kind, value in cells)))
                before = table_fingerprint(original, name, columns)
                after = table_fingerprint(converted, name, columns)
                if before != after:
                    raise ArchiveMigrationError(f"typed authority preservation failed: {name}")
                preserved.append(asdict(before))
            for identity, in converted.execute("SELECT input_id FROM editorial_inputs"):
                register_frozen_version(converted, "input", identity, 1)
            for identity, in converted.execute("SELECT edition_id FROM publication_editions"):
                register_frozen_version(converted, "content", identity, 1)
            if converted.execute("PRAGMA foreign_key_check").fetchone():
                raise ArchiveMigrationError("converted archive has invalid foreign keys")
            converted.commit()
        except BaseException:
            converted.rollback()
            raise
    return preserved


def _recover_target(database: Path, migration_id: str) -> list[dict]:
    """Only staging may change. Capture every permitted field before and after."""
    changes = []
    now = int(time.time())
    specs = (
        ("workflow_attempts", "attempt_id", "outcome='running'", "outcome='failed',finished_at=max(started_at,?),error_code='migration_interrupted'", (now,)),
        ("workflow_jobs", "job_id", "status='running'", "status='queued',lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=?,last_error_code='migration_interrupted'", (now, now)),
        ("ingestion_runs", "run_id", "outcome='running'", "outcome='failed',finished_at=max(started_at,?)", (now,)),
        ("acquisition_runs", "run_id", "outcome='running'", "outcome='failed',finished_at=max(started_at,?)", (now,)),
        ("editorial_model_calls", "call_id", "finished_at IS NULL", "finished_at=max(started_at,?),error_code='migration_interrupted'", (now,)),
    )
    with closing(connect_database(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM workflow_jobs WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at>?)", (now,)).fetchone():
                raise ArchiveMigrationError("target recovery found a live or unknown lease")
            for table, key, where, setters, parameters in specs:
                before = [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid")]
                connection.execute(f"UPDATE {table} SET {setters} WHERE {where}", parameters)
                for row in before:
                    after = dict(connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (row[key],)).fetchone())
                    changed = {field: {"before": value, "after": after[field]} for field, value in row.items() if after[field] != value}
                    changes.append({"migration_id": migration_id, "table": table, "identity": row[key], "fields": changed,
                                    "reason": "confirmed_offline_interruption"})
    return changes


def _mapping_file(database: Path, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(database, readonly=True)) as connection, path.open("xb") as output:
        queries = (
            ("creator", "SELECT external_id AS legacy_mid,creator_id FROM source_creators WHERE platform='bilibili' ORDER BY creator_id"),
            ("video", "SELECT external_id AS legacy_bvid,source_video_id FROM source_videos WHERE platform='bilibili' ORDER BY source_video_id"),
            ("part", "SELECT video_part_id,bvid AS legacy_bvid,page_index,source_video_id FROM video_parts ORDER BY video_part_id"),
        )
        for kind, sql in queries:
            for row in connection.execute(sql):
                output.write((canonical({"kind": kind, **dict(row)}) + "\n").encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())
    with path.open("rb") as source:
        size, digest = stream_hash(source)
    return {"size": size, "sha256": digest}


def _validate_files(root: Path) -> None:
    for relative, expected in required_artifacts(root / "archive.db").items():
        path = root.joinpath(*portable_artifact_parts(relative))
        require_regular_file(path)
        with path.open("rb") as source:
            _, digest = stream_hash(source)
        if expected is not None and expected != digest:
            raise ArchiveMigrationError("converted historical artifact hash mismatch")


# These are current facts/control state, not immutable history. A verification
# after cutover reports their deltas while still verifying original identity
# and frozen columns. Terminal historical payloads/bytes remain protected.
_MUTABLE_COLUMNS = {
    "bilibili_users": {"display_name","updated_at"},
    "videos": {"aid","mid","title","pubdate","updated_at"},
    "video_parts": {"cid","title","duration_ms","processing_status","updated_at"},
    "video_details": {"pic","desc","tid","observed_at"},
    "video_tags": {"bvid","tag_id","tag_name","tag_type"},
    "video_tag_observations": {"state","observed_at","error_code","run_id"},
    "ingestion_cursors": {"next_page","observed_total","state","last_error_code","updated_at"},
    "audio_objects": {"duration_ms"},
    "publication_edition_reviews": {"status","actor","note","issue_url","updated_at"},
    "publication_releases": {"status"},
    "publication_heads": {"current_edition_id","current_release_id"},
    "workflow_jobs": {"policy_key","status","priority","available_at","lease_owner","lease_expires_at","attempt_count","last_error_code","updated_at"},
    "workflow_job_dependencies": {"job_id","prerequisite_job_id"},
    "workflow_publications": {"published_at","artifact_json"},
}
_REPLACEABLE_ROWS = frozenset({"video_tags","workflow_job_dependencies"})


def _publish_binding(name: str, columns: list[str], cells) -> dict | None:
    """Recognize only the existing per-part publication enqueue recipe.

    Other imported payloads, including unknown publication recipes, retain
    their complete typed byte digest. No private payload values enter the
    ledger. The recognized payload contains three integer identity fields.
    """
    if name != "workflow_jobs":
        return None
    values = dict(zip(columns, cells))
    part_id = values["video_part_id"][1]
    if (values["kind"] != ("text", b"publish")
            or values["payload_json"][0] != "text"
            or type(part_id) is not int or part_id <= 0
            or values["dedupe_key"] != ("text", f"publish:{part_id}".encode("ascii"))):
        return None
    try:
        payload = json.loads(values["payload_json"][1])
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(payload, dict)
            or set(payload) != {"schema_version", "video_part_id", "transcript_id"}
            or any(type(value) is not int for value in payload.values())
            or payload["schema_version"] != 1 or payload["video_part_id"] != part_id
            or not 0 < payload["transcript_id"] < 2**63
            or values["payload_json"][1] != canonical(payload).encode("utf-8")):
        return None
    return {"video_part_id": part_id, "transcript_id": payload["transcript_id"]}


def _check_publish_binding(connection, rowid: int, cells, binding: dict) -> None:
    columns = legacy_source_contract()["objects"]["workflow_jobs"]["columns"]
    actual = typed_row_digest("workflow_jobs", rowid, cells, columns=["payload_json"])
    if actual == binding["payload_sha256"]:
        return
    candidate = _publish_binding("workflow_jobs", columns, cells)
    raw = cells[columns.index("payload_json")][1]
    if (candidate is None or candidate["video_part_id"] != binding["video_part_id"]
            or candidate["transcript_id"] == binding["transcript_id"]):
        raise ArchiveMigrationError("imported publication request identity changed")
    payload = json.loads(raw)
    if (raw != canonical(payload).encode("utf-8")
            or not connection.execute("SELECT 1 FROM transcripts WHERE transcript_id=? AND video_part_id=?",
                (candidate["transcript_id"], binding["video_part_id"])).fetchone()):
        raise ArchiveMigrationError("imported publication request identity changed")


def _row_ledger(database: Path, path: Path) -> dict:
    """Persist one hash per imported row without storing private source values."""
    path.parent.mkdir(parents=True,exist_ok=True)
    count = 0
    with closing(connect_database(database,readonly=True)) as connection, path.open("xb") as output:
        for name,shape in sorted(legacy_source_contract()["objects"].items()):
            if shape["kind"] != "table":
                continue
            protected = [column for column in shape["columns"] if column not in _MUTABLE_COLUMNS.get(name,set())]
            for rowid,cells in iter_legacy_rows(connection,name):
                row_protected = protected
                payload_binding = _publish_binding(name, shape["columns"], cells)
                if payload_binding is not None:
                    row_protected = [column for column in protected if column != "payload_json"]
                    payload_binding["payload_sha256"] = typed_row_digest(name, rowid, cells, columns=["payload_json"])
                item = {"table":name,"rowid":rowid,"sha256":typed_row_digest(name,rowid,cells),
                        "protected_columns":row_protected,"protected_sha256":typed_row_digest(name,rowid,cells,columns=row_protected)}
                if payload_binding is not None:
                    item["publish_request_binding"] = payload_binding
                output.write((canonical(item)+"\n").encode("utf-8")); count += 1
        output.flush(); os.fsync(output.fileno())
    with path.open("rb") as stream:
        size,sha256 = stream_hash(stream)
    return {"size":size,"sha256":sha256,"row_count":count,"format":"imported-typed-rows-v1"}


def _check_row_ledger(connection, path: Path, ledger: dict) -> dict:
    require_regular_file(path)
    with path.open("rb") as stream:
        if stream_hash(stream) != (ledger["size"],ledger["sha256"]):
            raise ArchiveMigrationError("migration typed-row ledger changed")
    deltas, count, current_name, iterator, current = {},0,None,None,None
    try:
        with path.open("r",encoding="utf-8") as source:
            for line in source:
                item = json.loads(line); name = item["table"]
                if name != current_name:
                    if iterator is not None:
                        iterator.close()
                    iterator = iter_legacy_rows(connection,name)
                    current = next(iterator,None); current_name = name
                while current is not None and current[0] < item["rowid"]:
                    current = next(iterator,None)
                expected_columns = [column for column in legacy_source_contract()["objects"][name]["columns"]
                                    if column not in _MUTABLE_COLUMNS.get(name,set())]
                binding = item.get("publish_request_binding")
                if binding is not None:
                    if name != "workflow_jobs":
                        raise ArchiveMigrationError("invalid publication migration ledger binding")
                    expected_columns.remove("payload_json")
                if item["protected_columns"] != expected_columns:
                    raise ArchiveMigrationError("migration ledger protection policy changed")
                missing = current is None or current[0] != item["rowid"]
                if missing:
                    if name not in _REPLACEABLE_ROWS:
                        raise ArchiveMigrationError(f"imported authority row missing: {name}")
                else:
                    rowid,cells = current
                    if binding is not None:
                        _check_publish_binding(connection, rowid, cells, binding)
                    if typed_row_digest(name,rowid,cells,columns=expected_columns) != item["protected_sha256"]:
                        raise ArchiveMigrationError(f"imported frozen authority changed: {name}")
                if missing or typed_row_digest(name,*current) != item["sha256"]:
                    deltas[name] = deltas.get(name,0)+1
                count += 1
    finally:
        if iterator is not None:
            iterator.close()
    if count != ledger["row_count"]:
        raise ArchiveMigrationError("migration typed-row ledger count changed")
    return deltas


def migrate_archive(source_root: Path, target_root: Path, *, artifact_root: Path | None = None,
                    dry_run: bool = False, expected_fingerprint: str | None = None,
                    source_evidence: dict | None = None) -> dict:
    """Convert one stopped legacy source into a separate empty v2 target."""
    source, target = (Path(os.path.abspath(path)) for path in (source_root, target_root))
    product = source if artifact_root is None else Path(os.path.abspath(artifact_root))
    for path in (source, target, product):
        require_no_links(path)
    if any(target == base or target.is_relative_to(base) or base.is_relative_to(target) for base in (source, product)):
        raise ArchiveMigrationError("migration source/artifact roots and target must be disjoint")
    with ExitStack() as access:
        for root in sorted((source, target), key=lambda path: os.path.normcase(str(path))):
            access.enter_context(ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access(allow_missing=root == target))
        report = migration_preflight(source, artifact_root=artifact_root)
        fingerprint = report["source"]["fingerprint"]
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise ArchiveMigrationError("migration source fingerprint differs from the approved plan")
        if source_evidence is not None:
            report["source"] = {**report["source"],"origin":source_evidence}
        if target.exists() and any(target.iterdir()):
            checked = check_migrated_archive(target)
            if checked["source_fingerprint"] != fingerprint:
                raise ArchiveMigrationError("nonempty target belongs to a different migration source")
            return {**checked,"operation":"migration-dry-run" if dry_run else "archive-migrate",
                    "reused":True,"conversion_performed":False,"target_created":False,"source_unchanged":True}
        if dry_run:
            return {**report, "operation": "migration-dry-run", "target_contract": UNIVERSAL_V2,
                    "target_root": str(target), "source_unchanged": True}
        _empty_target(target)
        required = report["totals"]["file_bytes"] + (source / "archive.db").stat().st_size * 3 + 16 * 1024 * 1024
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < required:
            raise ArchiveMigrationError("insufficient disk space for backup, conversion and artifacts")
        migration_id = hashlib.sha256((report["source"]["contract_id"] + ":" + UNIVERSAL_V2 + ":" + fingerprint).encode()).hexdigest()
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.migration-stage-", dir=target.parent) as temporary:
            owned = Path(temporary)
            stage = owned / "archive"
            stage.mkdir()
            backup = owned / "legacy.db"
            _backup_legacy(source / "archive.db", backup)
            preserved = _copy_authority(backup, stage / "archive.db")
            for item in report["files"]:
                original = Path(item["base"]).joinpath(*portable_artifact_parts(item["path"]))
                require_regular_file(original)
                destination = stage.joinpath(*portable_artifact_parts(item["path"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with original.open("rb") as reader, destination.open("xb") as writer:
                    size, digest = stream_hash(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                if (size, digest) != (item["size"], item["sha256"]):
                    raise ArchiveMigrationError("source artifact changed during conversion")
            validate_snapshot_database(stage / "archive.db")
            _validate_files(stage)
            recovery = _recover_target(stage / "archive.db", migration_id)
            mapping_path = f"documents/migrations/{migration_id}/id-mapping.jsonl"
            mapping = _mapping_file(stage / "archive.db", stage.joinpath(*portable_artifact_parts(mapping_path)))
            ledger_path = f"documents/migrations/{migration_id}/imported-rows.jsonl"
            ledger = _row_ledger(stage / "archive.db",stage.joinpath(*portable_artifact_parts(ledger_path)))
            completed = {"operation": "archive-migrate", "report_version": 1, "migration_id": migration_id,
                "source": report["source"], "target_contract": UNIVERSAL_V2,
                "preserved_tables": preserved, "files": report["files"], "excluded": report["excluded"],
                "shadowed": report["shadowed"], "recovery_changes": recovery,
                "id_mapping": {"path": mapping_path, **mapping}, "source_unchanged": True,
                "imported_rows": {"path":ledger_path,**ledger},
                "conversion_performed": True, "target_created": True, "completed_at": int(time.time())}
            encoded = canonical(completed)
            with closing(connect_database(stage / "archive.db")) as connection:
                with connection:
                    connection.execute("INSERT INTO migration_records VALUES (?,?,?,?,?,?)", (migration_id, fingerprint,
                        report["source"]["contract_id"], UNIVERSAL_V2, encoded, completed["completed_at"]))
            report_path = stage / "documents" / "migrations" / migration_id / "migration-report.json"
            with report_path.open("xb") as output:
                output.write((encoded + "\n").encode("utf-8")); output.flush(); os.fsync(output.fileno())
            validate_snapshot_database(stage / "archive.db")
            _validate_files(stage)
            if migration_preflight(source, artifact_root=artifact_root)["source"]["fingerprint"] != fingerprint:
                raise ArchiveMigrationError("source changed before target installation")
            # Windows fsync requires a writable descriptor for this owned target.
            with (stage / "archive.db").open("r+b") as database:
                os.fsync(database.fileno())
            for directory, _, _ in os.walk(stage, topdown=False):
                _sync_directory(Path(directory))
            _empty_target(target)
            if target.exists():
                target.rmdir()
            stage.rename(target)
            try:
                _sync_directory(target.parent)
            except OSError:
                # The target is installed and its own files/directories were
                # synced. A parent-directory sync failure cannot undo rename.
                return {**completed, "installed": True,
                        "warnings": ["target_parent_directory_sync_failed"]}
            return completed


def initialize_archive(target_root: Path, *, with_source_observations: bool = False) -> dict:
    """Explicit empty-target initialization with the same complete install rule."""
    target = Path(os.path.abspath(target_root))
    _empty_target(target)
    with ArchiveSession(target, mode=ArchiveAccessMode.MAINTENANCE).access(allow_missing=True):
        _empty_target(target)
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.bootstrap-", dir=target.parent) as temporary:
            stage = Path(temporary) / "archive"
            stage.mkdir()
            with closing(connect_database(stage / "archive.db")) as connection:
                bootstrap_contract(connection)
                if with_source_observations:
                    from bili_asr.storage.source_observations import install_observations_in_staged_copy
                    install_observations_in_staged_copy(connection)
            validate_snapshot_database(stage / "archive.db")
            # Windows fsync requires a writable descriptor for this owned target.
            with (stage / "archive.db").open("r+b") as database:
                os.fsync(database.fileno())
            _sync_directory(stage)
            _empty_target(target)
            if target.exists():
                target.rmdir()
            stage.rename(target)
            try:
                _sync_directory(target.parent)
            except OSError:
                return {"operation": "archive-init", "target_contract": UNIVERSAL_V2,
                        "archive_root": str(target), "installed": True,
                        "warnings": ["target_parent_directory_sync_failed"]}
    return {"operation": "archive-init", "target_contract": UNIVERSAL_V2, "archive_root": str(target)}


def check_migrated_archive(target_root: Path) -> dict:
    """Verify imported frozen rows/files and classify mutable baseline deltas.

    New rows after cutover are outside the imported row ledger. Metadata,
    mutable bundle slots and execution state may evolve; these changes are
    reported, not confused with corruption of historical frozen objects.
    """
    target = Path(os.path.abspath(target_root))
    with ArchiveSession(target, mode=ArchiveAccessMode.READ) as session:
        rows = session.connection.execute("SELECT * FROM migration_records ORDER BY migration_id").fetchall()
        if len(rows) != 1:
            raise ArchiveMigrationError("target does not contain one completed migration record")
        record = rows[0]
        report = json.loads(record["report_json"])
        if (report["migration_id"] != record["migration_id"] or report["source"]["fingerprint"] != record["source_fingerprint"]
                or canonical(report) != record["report_json"]):
            raise ArchiveMigrationError("migration audit identity mismatch")
        report_path = target / "documents" / "migrations" / record["migration_id"] / "migration-report.json"
        require_regular_file(report_path)
        with report_path.open("rb") as source:
            expected_report = (record["report_json"] + "\n").encode("utf-8")
            if stream_hash(source) != (len(expected_report), hashlib.sha256(expected_report).hexdigest()):
                raise ArchiveMigrationError("migration audit report changed")
        changed_files = []
        for item in report["files"]:
            path = target.joinpath(*portable_artifact_parts(item["path"]))
            try:
                require_regular_file(path)
                with path.open("rb") as source:
                    actual = stream_hash(source)
            except FileNotFoundError:
                actual = None
            if actual != (item["size"], item["sha256"]):
                if item["path"].split("/",1)[0] in {"audio","documents","publications"}:
                    raise ArchiveMigrationError("preserved frozen source artifact changed")
                changed_files.append(item["path"])
        mapping = report["id_mapping"]
        path = target.joinpath(*portable_artifact_parts(mapping["path"]))
        require_regular_file(path)
        with path.open("rb") as source:
            if stream_hash(source) != (mapping["size"], mapping["sha256"]):
                raise ArchiveMigrationError("migration identity mapping changed")
        ledger = report["imported_rows"]
        deltas = _check_row_ledger(session.connection,target.joinpath(*portable_artifact_parts(ledger["path"])),ledger)
        validate_snapshot_database(target / "archive.db")
        _validate_files(target)
        return {"operation": "migration-check", "valid": True, "migration_id": record["migration_id"],
                "source_fingerprint": record["source_fingerprint"], "target_contract": UNIVERSAL_V2,
                "imported_baseline_matches":not deltas and not changed_files,
                "current_fact_deltas":deltas,"mutable_artifact_deltas":changed_files}


def migrate_snapshot(snapshot: Path, target_root: Path, *, dry_run: bool = False,
                     expected_fingerprint: str | None = None) -> dict:
    """Convert a verified legacy snapshot, never restoring it before conversion.

    Restoring would mutate running source history; extraction here is byte-only
    and uses the already validated member list from the public verified reader.
    """
    from bili_asr.services.archive_snapshot import verified_snapshot_reader
    snapshot = Path(snapshot).absolute()
    require_no_links(snapshot)
    require_regular_file(snapshot)
    with snapshot.open("rb") as stream:
        package_size,package_sha256 = stream_hash(stream)
    with verified_snapshot_reader(snapshot) as verified:
        inspect_migration_source(verified.database_path)
        with tempfile.TemporaryDirectory(prefix="bili-asr-legacy-snapshot-source-") as temporary:
            source_root = Path(temporary) / "archive"
            source_root.mkdir()
            with zipfile.ZipFile(snapshot) as package:
                for item in verified.manifest["files"]:
                    target = source_root.joinpath(*portable_artifact_parts(item["path"]))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item["path"]) as reader, target.open("xb") as writer:
                        size, sha256 = stream_hash(reader, writer)
                    if (size, sha256) != (item["size"], item["sha256"]):
                        raise ArchiveMigrationError("legacy snapshot changed during extraction")
            with snapshot.open("rb") as stream:
                if stream_hash(stream) != (package_size,package_sha256):
                    raise ArchiveMigrationError("legacy snapshot changed before conversion")
            return migrate_archive(source_root, target_root, dry_run=dry_run, expected_fingerprint=expected_fingerprint,
                source_evidence={"kind":"verified-snapshot","path":str(snapshot),"size":package_size,"sha256":package_sha256})
