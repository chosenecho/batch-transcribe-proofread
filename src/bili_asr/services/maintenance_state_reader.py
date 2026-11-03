"""Offline, bounded views of inactive task-state evidence; never restore jobs."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from bili_asr.services.maintenance_media import media_availability
from bili_asr.services.maintenance_task_state import extract_task_state
from bili_asr.storage.archive_contracts import runtime_contract
from bili_asr.storage.upgrade_evidence import readonly

MAX_REPORT_BYTES = 16 * 1024 * 1024
_ADAPTERS = {"bilibili-v1": "bilibili-task-state-reader/v1",
             "universal-v2": "universal-task-state-reader/v2"}


@dataclass(frozen=True)
class StateQuery:
    """Exact matches are OR within one field, AND across different fields."""

    job_ids: tuple[str, ...] = ()
    videos: tuple[str, ...] = ()
    part_ids: tuple[int, ...] = ()
    stages: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    updated_from: int | None = None
    updated_to: int | None = None
    offset: int = 0
    limit: int = 20
    history_limit: int = 1000

    def validate(self) -> None:
        for name in ("job_ids", "videos", "stages", "statuses", "models"):
            values = getattr(self, name)
            if (not isinstance(values, tuple) or len(values) > 100
                    or any(not isinstance(value, str) or not value or len(value) > 1024 for value in values)):
                raise ValueError("invalid task-state filter: " + name)
        if (not isinstance(self.part_ids, tuple) or len(self.part_ids) > 100
                or any(type(value) is not int or value < 1 for value in self.part_ids)):
            raise ValueError("invalid task-state part filter")
        for name, minimum, maximum in (("offset", 0, 1000000), ("limit", 1, 100), ("history_limit", 1, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid task-state pagination: " + name)
        for value in (self.updated_from, self.updated_to):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid task-state time range")
        if self.updated_from is not None and self.updated_to is not None and self.updated_from > self.updated_to:
            raise ValueError("reversed task-state time range")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _scratch(work_directory: Path | None, prefix: str):
    if work_directory is not None and (not work_directory.is_dir() or work_directory.is_symlink()):
        raise ValueError("task-state reader scratch must be an existing ordinary directory")
    return tempfile.TemporaryDirectory(prefix=prefix, dir=work_directory)


def _bounded_report(value: dict) -> dict:
    if len(_canonical(value)) > MAX_REPORT_BYTES:
        raise ValueError("task-state report exceeds byte budget; narrow filters or limit")
    return value


def _record(row: sqlite3.Row) -> dict:
    values = {name: ({"sqlite_type": "blob", "base64": base64.b64encode(value).decode("ascii")}
                     if isinstance(value, bytes) else value) for name, value in dict(row).items()}
    return {"values": values, "row_sha256": hashlib.sha256(_canonical(values)).hexdigest(),
            "frozen_json_sha256": {name: hashlib.sha256(value.encode("utf-8")).hexdigest()
                                   for name, value in dict(row).items()
                                   if name.endswith("_json") and isinstance(value, str)}}


def _job_model_filter(alias: str, models: tuple[str, ...]) -> tuple[str, list[str]]:
    markers = ",".join("?" for _ in models)
    condition = ("(EXISTS (SELECT 1 FROM editorial_model_calls m JOIN workflow_attempts a USING(attempt_id) "
                 "WHERE a.job_id=" + alias + ".job_id AND CASE WHEN json_valid(m.request_json) THEN json_extract(m.request_json,'$.model') END IN (" + markers + ")) "
                 "OR EXISTS (SELECT 1 FROM workflow_asr_profiles f WHERE f.profile_id=" + alias + ".profile_id AND f.model_name IN (" + markers + ")))" )
    return condition, [*models, *models]


def _where(query: StateQuery, contract: str) -> tuple[str, list]:
    conditions = []
    arguments: list = []
    for expression, values in (("j.job_id", query.job_ids), ("j.video_part_id", query.part_ids),
                               ("j.kind", query.stages), ("j.status", query.statuses)):
        if values:
            conditions.append(expression + " IN (" + ",".join("?" for _ in values) + ")")
            arguments.extend(values)
    if query.videos:
        expression = "p.bvid"
        if contract == "universal-v2":
            expression = "coalesce(p.bvid,(SELECT platform || ':' || external_id FROM source_videos WHERE source_video_id=p.source_video_id))"
        conditions.append(expression + " IN (" + ",".join("?" for _ in query.videos) + ")")
        arguments.extend(query.videos)
    if query.models:
        condition, model_arguments = _job_model_filter("j", query.models)
        conditions.append(condition)
        arguments.extend(model_arguments)
    for operator, value in ((">=", query.updated_from), ("<=", query.updated_to)):
        if value is not None:
            conditions.append("j.updated_at" + operator + "?")
            arguments.append(value)
    return (" AND ".join(conditions) or "1"), arguments


def _read_records(connection: sqlite3.Connection, sql: str, arguments: list, limit: int, budget: list[int], offset: int = 0) -> dict:
    records = []
    for row in connection.execute(sql + " LIMIT ? OFFSET ?", [*arguments, limit + 1, offset]):
        if len(records) == limit:
            return {"records": records, "truncated": True}
        record = _record(row)
        budget[0] += len(_canonical(record))
        if budget[0] > MAX_REPORT_BYTES:
            raise ValueError("task-state report exceeds byte budget; narrow filters or history-limit")
        records.append(record)
    return {"records": records, "truncated": False}


def _history(connection: sqlite3.Connection, jobs: list[str], parts: list[int], limit: int, budget: list[int]) -> dict:
    """Fixed relation names, bounded queries, original rowid insertion order."""
    job_marks = ",".join("?" for _ in jobs) or "NULL"
    part_marks = ",".join("?" for _ in parts) or "NULL"
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    histories = {}
    for table in sorted(tables):
        if table.startswith(("sqlite_", "transcript_search")) or table == "workflow_jobs":
            continue
        identifier = '"' + table.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute("PRAGMA table_info(" + identifier + ")")}
        if "job_id" in columns:
            condition, arguments = "job_id IN (" + job_marks + ")", jobs
        elif "video_part_id" in columns:
            condition, arguments = "video_part_id IN (" + part_marks + ")", parts
        elif "attempt_id" in columns:
            condition = "attempt_id IN (SELECT attempt_id FROM workflow_attempts WHERE job_id IN (" + job_marks + "))"
            arguments = jobs
        elif "input_id" in columns:
            condition = "input_id IN (SELECT input_id FROM editorial_job_inputs WHERE job_id IN (" + job_marks + "))"
            arguments = jobs
        elif "transcript_id" in columns:
            condition = "transcript_id IN (SELECT transcript_id FROM transcripts WHERE video_part_id IN (" + part_marks + "))"
            arguments = parts
        elif "revision_id" in columns:
            condition = "revision_id IN (SELECT revision_id FROM editorial_revisions WHERE job_id IN (" + job_marks + "))"
            arguments = jobs
        elif "edition_id" in columns:
            condition = "edition_id IN (SELECT edition_id FROM publication_editions WHERE video_part_id IN (" + part_marks + "))"
            arguments = parts
        else:
            # Run summaries, model catalog and policies have no part ownership.
            condition, arguments = "1", []
        histories[table] = _read_records(connection, "SELECT rowid AS archive_rowid,* FROM " + identifier
                                         + " WHERE " + condition + " ORDER BY rowid", arguments, limit, budget)
    return histories


def read_task_state(snapshot: Path, *, query: StateQuery | None = None, work_directory: Path | None = None) -> dict:
    """Extract into private scratch space, validate, read, then remove scratch."""
    query = query or StateQuery()
    query.validate()
    with _scratch(work_directory, "task-state-reader-") as temporary:
        stage = Path(temporary)
        manifest = extract_task_state(Path(snapshot), stage)
        contract = manifest["runtime_contract"]
        if contract not in _ADAPTERS:
            raise ValueError("unsupported task-state reader contract")
        with closing(readonly(stage / "archive.db")) as connection:
            if runtime_contract(connection) != contract:
                raise ValueError("task-state runtime contract mismatch")
            connection.row_factory = sqlite3.Row
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_REPORT_BYTES)
            connection.set_progress_handler(lambda: 1, 2000000)
            where, arguments = _where(query, contract)
            selected = list(connection.execute("SELECT j.rowid AS archive_rowid,j.* FROM workflow_jobs j "
                            "LEFT JOIN video_parts p USING(video_part_id) WHERE " + where
                            + " ORDER BY j.rowid LIMIT ? OFFSET ?", [*arguments, query.limit + 1, query.offset]))
            more = len(selected) > query.limit
            selected = selected[:query.limit]
            budget = [0]
            jobs = []
            for row in selected:
                record = _record(row)
                budget[0] += len(_canonical(record))
                if budget[0] > MAX_REPORT_BYTES:
                    raise ValueError("task-state report exceeds byte budget; narrow filters or limit")
                jobs.append(record)
            histories = _history(connection, [row["job_id"] for row in selected],
                                 sorted({row["video_part_id"] for row in selected if row["video_part_id"] is not None}),
                                 query.history_limit, budget)
        controls = []
        for entry in manifest["files"]:
            if entry["path"].startswith("control-state/"):
                raw = (stage / entry["path"]).read_bytes()
                budget[0] += len(raw)
                if budget[0] > MAX_REPORT_BYTES:
                    raise ValueError("task-state control report exceeds byte budget")
                controls.append({**entry, "base64": base64.b64encode(raw).decode("ascii")})
        return _bounded_report({"operation": "task-state-read", "format": "archive-task-state-reader/v1", "valid": True,
                "adapter": _ADAPTERS[contract], "content_id": manifest["content_id"], "inactive_archive": True,
                "restore_policy": manifest["restore_policy"], "observation": manifest["observation"],
                "media_self_contained": manifest["media_self_contained"], "media_dependencies": manifest["media_dependencies"],
                **media_availability(manifest),
                "query": asdict(query), "jobs": jobs, "histories": histories, "control_records": controls,
                "pagination": {"offset": query.offset, "limit": query.limit, "next_offset": query.offset + len(jobs) if more else None},
                "evidence_files": [entry for entry in manifest["files"] if entry["path"] != "archive.db"]})


def read_state_evidence(snapshot: Path, path: str, *, work_directory: Path | None = None) -> dict:
    """Return a verified member's original bytes without opening active archives."""
    if not isinstance(path, str) or not path or path == "archive.db":
        raise ValueError("task-state evidence must name a declared evidence member")
    with _scratch(work_directory, "task-state-evidence-") as temporary:
        stage = Path(temporary)
        manifest = extract_task_state(Path(snapshot), stage)
        entry = next((record for record in manifest["files"] if record["path"] == path), None)
        if entry is None or entry["size"] > MAX_REPORT_BYTES:
            raise ValueError("task-state evidence missing or exceeds byte budget")
        raw = (stage / entry["path"]).read_bytes()
        return _bounded_report({"operation": "task-state-evidence-read", "valid": True, "content_id": manifest["content_id"],
                "inactive_archive": True, "evidence": {**entry, "base64": base64.b64encode(raw).decode("ascii")}})


def _record_filter(connection: sqlite3.Connection, table: str, query: StateQuery, contract: str) -> tuple[str, list]:
    """Explicit column/relation adapters refuse filters with no defined meaning."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')}
    conditions = []
    arguments: list = []
    part = "r.video_part_id" if "video_part_id" in columns else None
    if part is None:
        for key, relation in (("transcript_id", "transcripts"), ("input_id", "editorial_inputs"),
                              ("edition_id", "publication_editions")):
            if key in columns:
                part = "(SELECT video_part_id FROM " + relation + " WHERE " + key + "=r." + key + ")"
                break
        if part is None and "revision_id" in columns:
            part = "(SELECT i.video_part_id FROM editorial_revisions v JOIN editorial_inputs i USING(input_id) WHERE v.revision_id=r.revision_id)"
    if part is None and "job_id" in columns:
        part = "(SELECT video_part_id FROM workflow_jobs WHERE job_id=r.job_id)"
    if part is None and "attempt_id" in columns:
        part = "(SELECT j.video_part_id FROM workflow_jobs j JOIN workflow_attempts a USING(job_id) WHERE a.attempt_id=r.attempt_id)"
    job = "r.job_id" if "job_id" in columns else None
    if job is None and "attempt_id" in columns:
        job = "(SELECT job_id FROM workflow_attempts WHERE attempt_id=r.attempt_id)"
    if job is None and "input_id" in columns and query.job_ids:
        conditions.append("EXISTS (SELECT 1 FROM editorial_job_inputs b WHERE b.input_id=r.input_id AND b.job_id IN ("
                          + ",".join("?" for _ in query.job_ids) + "))")
        arguments.extend(query.job_ids)
        job_values = ()
    else:
        job_values = query.job_ids
    for expression, values, label in ((job, job_values, "job"),
                                      (part, query.part_ids, "part"), ("r.kind" if "kind" in columns else None, query.stages, "stage")):
        if values:
            if expression is None:
                raise ValueError("task-state table has no " + label + " filter: " + table)
            conditions.append(expression + " IN (" + ",".join("?" for _ in values) + ")")
            arguments.extend(values)
    if query.videos:
        if "bvid" in columns:
            expression = "r.bvid"
            if table == "video_parts" and contract == "universal-v2":
                expression = "coalesce(r.bvid,(SELECT platform || ':' || external_id FROM source_videos WHERE source_video_id=r.source_video_id))"
        elif table == "source_videos":
            expression = "r.platform || ':' || r.external_id"
        elif table == "acquisition_runs":
            expression = "CASE WHEN r.selector_kind IN ('bvid','source-ref') THEN r.selector_target END"
        elif part is not None:
            expression = "(SELECT p.bvid FROM video_parts p WHERE p.video_part_id=" + part + ")"
            if contract == "universal-v2":
                expression = "(SELECT coalesce(p.bvid,v.platform || ':' || v.external_id) FROM video_parts p LEFT JOIN source_videos v USING(source_video_id) WHERE p.video_part_id=" + part + ")"
        else:
            raise ValueError("task-state table has no video filter: " + table)
        conditions.append(expression + " IN (" + ",".join("?" for _ in query.videos) + ")")
        arguments.extend(query.videos)
    if query.statuses:
        status = next((name for name in ("status", "outcome", "quality_status", "processing_status", "to_status") if name in columns), None)
        if status is None:
            raise ValueError("task-state table has no status filter: " + table)
        conditions.append('r."' + status + '" IN (' + ",".join("?" for _ in query.statuses) + ")")
        arguments.extend(query.statuses)
    if query.updated_from is not None or query.updated_to is not None:
        timestamp = next((name for name in ("updated_at", "finished_at", "started_at", "created_at", "published_at", "assessed_at", "changed_at") if name in columns), None)
        if timestamp is None:
            raise ValueError("task-state table has no time filter: " + table)
        for operator, value in ((">=", query.updated_from), ("<=", query.updated_to)):
            if value is not None:
                expression = 'r."' + timestamp + '"'
                if timestamp == "finished_at" and "started_at" in columns:
                    expression = "coalesce(r.finished_at,r.started_at)"
                conditions.append(expression + operator + "?")
                arguments.append(value)
    if query.models:
        expression = None
        if table == "workflow_jobs":
            condition, model_arguments = _job_model_filter("r", query.models)
            conditions.append(condition)
            arguments.extend(model_arguments)
            return " AND ".join(conditions), arguments
        for key, candidate in (("model_name", "r.model_name"), ("model_id", "(SELECT model_name FROM asr_models WHERE model_id=r.model_id)"),
                               ("profile_id", "(SELECT model_name FROM workflow_asr_profiles WHERE profile_id=r.profile_id)"),
                               ("request_json", "CASE WHEN json_valid(r.request_json) THEN json_extract(r.request_json,'$.model') END")):
            if key in columns:
                expression = candidate
                break
        if expression is None:
            raise ValueError("task-state table has no model filter: " + table)
        conditions.append(expression + " IN (" + ",".join("?" for _ in query.models) + ")")
        arguments.extend(query.models)
    return " AND ".join(conditions) or "1", arguments


def read_task_records(snapshot: Path, table: str, *, query: StateQuery | None = None,
                      work_directory: Path | None = None) -> dict:
    """Page original tables, including collection runs with no workflow job."""
    query = query or StateQuery()
    query.validate()
    with _scratch(work_directory, "task-state-records-") as temporary:
        stage = Path(temporary)
        manifest = extract_task_state(snapshot, stage)
        registered_tables = {item["name"] for item in manifest["tables"]}
        if not isinstance(table, str) or table not in registered_tables:
            raise ValueError("unsupported task-state record table")
        with closing(readonly(stage / "archive.db")) as connection:
            connection.row_factory = sqlite3.Row
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_REPORT_BYTES)
            connection.set_progress_handler(lambda: 1, 2000000)
            where, arguments = _record_filter(connection, table, query, manifest["runtime_contract"])
            result = _read_records(connection, 'SELECT r.rowid AS archive_rowid,r.* FROM "' + table.replace('"', '""')
                                   + '" r WHERE ' + where + " ORDER BY r.rowid", arguments, query.limit, [0], query.offset)
        return _bounded_report({"operation": "task-state-records-read", "valid": True, "inactive_archive": True,
                "content_id": manifest["content_id"], "adapter": _ADAPTERS[manifest["runtime_contract"]],
                "restore_policy": manifest["restore_policy"], "observation": manifest["observation"],
                "media_self_contained": manifest["media_self_contained"],
                **media_availability(manifest),
                "table": table, "query": asdict(query), **result,
                "pagination": {"offset": query.offset, "limit": query.limit,
                               "next_offset": query.offset + len(result["records"]) if result["truncated"] else None}})
