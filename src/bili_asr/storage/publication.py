"""Transactional publication heads, reviews and append-only audit events."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import re
import sqlite3
import time
from uuid import uuid4

from bili_asr.canonical_json import canonical, digest
from bili_asr.publication_content import normalize_content
from bili_asr.publication_identity import validate_release_identity
from bili_asr.storage.database import _validate_connection, require_manuscript_schema
from bili_asr.storage.archive_contracts import frozen_version, register_frozen_version


class PublicationConflictError(ValueError):
    """A caller's expected publication head or review state is stale."""


class PublicationRepository:
    def __init__(self, connection: sqlite3.Connection):
        _validate_connection(connection)
        require_manuscript_schema(connection)
        self.connection = connection

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise ValueError("publication-transaction: caller transaction must be completed")
        # Head changes and withdrawals must not race the export directory swap.
        from bili_asr.content_rights import distribution_lock
        with distribution_lock(self.connection, blocking=True):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def head(self, part_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM publication_heads WHERE video_part_id = ?", (part_id,)
        ).fetchone()
        return dict(row) if row else None

    def edition(self, edition_id: str) -> dict:
        row = self.connection.execute(
            "SELECT e.*, r.review_id, r.status AS review_status, "
            "r.content_sha256 AS review_content_sha256, r.actor AS review_actor, "
            "r.note AS review_note, r.issue_url, r.updated_at AS reviewed_at "
            "FROM publication_editions e JOIN publication_edition_reviews r "
            "ON r.edition_id = e.edition_id WHERE e.edition_id = ?", (edition_id,)
        ).fetchone()
        if row is None:
            raise ValueError("publication-edition: unknown edition ID or missing review")
        edition = dict(row)
        edition["content_version"] = frozen_version(self.connection, "content", edition_id)
        try:
            edition["content"] = json.loads(edition["content_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("publication-integrity: malformed edition JSON") from exc
        if (canonical(edition["content"]) != edition["content_json"]
                or digest(edition["content"]) != edition["content_sha256"]
                or edition["content_sha256"] != edition["review_content_sha256"]):
            raise ValueError("publication-integrity: edition or review content hash mismatch")
        review_event = self.connection.execute(
            "SELECT * FROM publication_events WHERE edition_id = ? AND event_type "
            "IN ('created', 'edited', 'in-review', 'changes-requested', 'approved', 'rejected') "
            "ORDER BY event_id DESC LIMIT 1", (edition_id,)
        ).fetchone()
        if (review_event is None or (
                review_event["to_status"] != edition["review_status"]
                or review_event["video_part_id"] != edition["video_part_id"]
                or review_event["review_id"] != edition["review_id"]
                or review_event["content_sha256"] != edition["content_sha256"]
                or review_event["actor"] != edition["review_actor"])):
            raise ValueError("publication-integrity: review state disagrees with audit event")
        if (review_event["event_type"] not in {"created", "edited"}
                and (review_event["note"] != edition["review_note"]
                     or review_event["issue_url"] != edition["issue_url"])):
            raise ValueError("publication-integrity: review details disagree with audit event")
        head = self.head(edition["video_part_id"])
        if head is None:
            raise ValueError("publication-integrity: edition part has no head")
        for key, table, identity in (
            ("current_edition_id", "publication_editions", "edition_id"),
            ("current_release_id", "publication_releases", "release_id"),
        ):
            value = head[key]
            if value is not None:
                target = self.connection.execute(
                    f"SELECT video_part_id FROM {table} WHERE {identity} = ?", (value,)
                ).fetchone()
                if target is None or target[0] != edition["video_part_id"]:
                    raise ValueError("publication-integrity: cross-part or missing head")
                if key == "current_release_id" and self.release(value)["status"] != "published":
                    raise ValueError("publication-integrity: effective head references an inactive release")
            edition[key] = value
        return edition

    def release(self, release_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM publication_releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        if row is None:
            raise ValueError("publication-release: unknown release ID")
        return dict(row)

    def release_for_edition(self, edition_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM publication_releases WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        return dict(row) if row else None

    def event(self, edition: dict, event_type: str, from_status: str | None,
              to_status: str, actor: str, note: str = "", issue_url: str | None = None,
              release_id: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO publication_events(video_part_id, edition_id, release_id, "
            "review_id, content_sha256, event_type, from_status, to_status, actor, "
            "issue_url, note, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (edition["video_part_id"], edition["edition_id"], release_id,
             edition["review_id"], edition["content_sha256"], event_type, from_status,
             to_status, actor, issue_url, note, int(time.time())),
        )

    def insert_edition(self, *, part_id: int, revision_id: str,
                       content: dict, parent_edition_id: str | None,
                       actor: str, note: str, event_type: str, content_version: int = 1) -> dict:
        """Caller holds the transaction and has verified frozen source identity."""
        head = self.head(part_id)
        current = head["current_edition_id"] if head else None
        if current != parent_edition_id:
            raise PublicationConflictError("publication-conflict: current edition differs from expected parent")
        if parent_edition_id:
            parent = self.edition(parent_edition_id)
            if parent["video_part_id"] != part_id:
                raise ValueError("publication-integrity: parent belongs to another part")
        edition_id, review_id, now = uuid4().hex, uuid4().hex, int(time.time())
        self.connection.execute(
            "INSERT INTO publication_editions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (edition_id, part_id, revision_id, parent_edition_id, canonical(content),
             digest(content), now, actor, note),
        )
        register_frozen_version(self.connection, "content", edition_id, content_version)
        self.connection.execute(
            "INSERT INTO publication_edition_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (edition_id, review_id, digest(content), "pending-review", actor, "", None, now),
        )
        if head:
            self.connection.execute(
                "UPDATE publication_heads SET current_edition_id = ? WHERE video_part_id = ?",
                (edition_id, part_id),
            )
        else:
            self.connection.execute("INSERT INTO publication_heads VALUES (?, ?, NULL)", (part_id, edition_id))
        edition = {"edition_id": edition_id, "video_part_id": part_id,
                   "review_id": review_id, "content_sha256": digest(content)}
        self.event(edition, event_type, None, "pending-review", actor, note)
        result = self.edition(edition_id)
        if event_type == "edited" and parent_edition_id:
            from bili_asr.storage.import_origins import inherit_import_origin
            inherit_import_origin(self.connection, parent_edition_id, result)
        return result

    def set_review(self, *, edition_id: str, status: str, content_sha256: str,
                   expected_status: str, actor: str, note: str,
                   issue_url: str | None) -> dict:
        transitions = {
            "pending-review": {"in-review"},
            "in-review": {"approved", "changes-requested", "rejected"},
            "changes-requested": {"in-review"},
            "approved": set(), "rejected": set(),
        }
        edition = self.edition(edition_id)
        current = edition["review_status"]
        if current != expected_status:
            raise PublicationConflictError("publication-conflict: review state differs from expected status")
        if edition["content_sha256"] != content_sha256:
            raise ValueError("publication-approval: reviewed content hash does not match edition")
        if status not in transitions.get(current, set()):
            raise ValueError(f"publication-review: invalid transition {current} -> {status}")
        self.connection.execute(
            "UPDATE publication_edition_reviews SET status = ?, actor = ?, note = ?, "
            "issue_url = ?, updated_at = ? WHERE edition_id = ? AND status = ?",
            (status, actor, note, issue_url, int(time.time()), edition_id, current),
        )
        self.event(edition, status, current, status, actor, note, issue_url)
        return self.edition(edition_id)

    def register_release(self, *, edition: dict, release: dict,
                         expected_release_id: str | None, actor: str) -> dict:
        head = self.head(edition["video_part_id"])
        if head is None or head["current_release_id"] != expected_release_id:
            raise PublicationConflictError("publication-conflict: effective release differs from expected release")
        if expected_release_id:
            previous = self.release(expected_release_id)
            if previous["status"] != "published" or previous["video_part_id"] != edition["video_part_id"]:
                raise ValueError("publication-integrity: previous release is not effective for this part")
            previous_edition = self.edition(previous["edition_id"])
            self.connection.execute(
                "UPDATE publication_releases SET status = 'superseded' WHERE release_id = ?",
                (expected_release_id,),
            )
            self.event(previous_edition, "superseded", "published", "superseded", actor,
                       release_id=expected_release_id)
        columns = ("release_id", "video_part_id", "edition_id", "review_id", "content_sha256",
                   "template_version", "relative_path", "artifact_sha256", "published_at", "published_by", "status")
        self.connection.execute(
            "INSERT INTO publication_releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(release[key] for key in columns),
        )
        self.connection.execute(
            "UPDATE publication_heads SET current_release_id = ? WHERE video_part_id = ?",
            (release["release_id"], edition["video_part_id"]),
        )
        self.event(edition, "published", None, "published", actor, release_id=release["release_id"])
        return self.release(release["release_id"])

    def withdraw(self, *, release_id: str, actor: str, note: str) -> dict:
        release = self.release(release_id)
        edition = self.edition(release["edition_id"])
        head_id = edition["current_release_id"]
        if release["status"] == "withdrawn":
            if head_id == release_id:
                raise ValueError("publication-integrity: withdrawn release is still effective")
            return {**release, "idempotent": True}
        if (release["status"] == "published") != (head_id == release_id):
            raise ValueError("publication-integrity: release state disagrees with effective head")
        self.connection.execute(
            "UPDATE publication_releases SET status = 'withdrawn' WHERE release_id = ?", (release_id,)
        )
        self.connection.execute(
            "UPDATE publication_heads SET current_release_id = NULL "
            "WHERE video_part_id = ? AND current_release_id = ?",
            (release["video_part_id"], release_id),
        )
        self.event(edition, "withdrawn", release["status"], "withdrawn", actor, note, release_id=release_id)
        return {**self.release(release_id), "idempotent": False}


_IDENTITY = re.compile(r"[A-Za-z0-9_-]+\Z")


def read_revision(connection: sqlite3.Connection, revision_id: str) -> tuple[dict, dict]:
    if not isinstance(revision_id, str) or not _IDENTITY.fullmatch(revision_id):
        raise ValueError("publication-revision: invalid revision ID")
    row = connection.execute(
        "SELECT r.*, i.video_part_id, i.base_transcript_id, i.reference_transcript_id, "
        "i.prepared_json FROM editorial_revisions r "
        "JOIN editorial_inputs i ON i.input_id = r.input_id WHERE r.revision_id = ?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("publication-revision: unknown revision ID")
    try:
        prepared = json.loads(row["prepared_json"])
        snapshot = prepared["snapshot"]
        blocks = json.loads(row["blocks_json"])
        metadata = snapshot["metadata"]
        if (prepared["input_id"] != row["input_id"] or digest(snapshot) != row["input_id"]
                or digest({"input_id": row["input_id"], "blocks": blocks}) != revision_id
                or snapshot["video_part_id"] != row["video_part_id"]
                or snapshot["base"]["transcript_id"] != row["base_transcript_id"]
                or (snapshot["reference"]["transcript_id"] if snapshot["reference"] else None)
                    != row["reference_transcript_id"]):
            raise ValueError("publication-integrity: frozen revision identity mismatch")
        for transcript_id in (row["base_transcript_id"], row["reference_transcript_id"]):
            if transcript_id is None:
                continue
            transcript = connection.execute(
                "SELECT video_part_id FROM transcripts WHERE transcript_id = ?", (transcript_id,)
            ).fetchone()
            if transcript is None or transcript[0] != row["video_part_id"]:
                raise ValueError("publication-integrity: frozen transcript belongs to another part")
        job = connection.execute(
            "SELECT video_part_id FROM workflow_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        if job is None or job[0] != row["video_part_id"]:
            raise ValueError("publication-integrity: frozen revision job belongs to another part")
        from bili_asr.storage.sources import SourceRepository
        live = SourceRepository(connection).part(row["video_part_id"])
        version = frozen_version(connection, "input", row["input_id"])
        if version == 2:
            from bili_asr.source_metadata import SourceMetadataSnapshot
            ref = SourceMetadataSnapshot.from_dict(metadata["source_metadata"]).ref
            matches = live["content_ref"] == ref
        else:
            matches = (live["bvid"], live["page_index"]) == (metadata["bvid"], metadata["page_index"])
        if not matches:
            raise ValueError("publication-integrity: frozen revision belongs to another video part")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("publication-integrity: invalid frozen revision") from exc
    return dict(row), prepared


def read_edition(connection: sqlite3.Connection, edition_id: str) -> dict:
    repository = PublicationRepository(connection)
    edition = repository.edition(edition_id)
    revision, prepared = read_revision(connection, edition["revision_id"])
    version = edition["content_version"]
    if version == 2:
        from bili_asr.publication_models_v2 import normalize_content_v2
        normalized = normalize_content_v2(edition["content"])
    else:
        normalized = normalize_content(edition["content"])
    metadata = prepared["snapshot"]["metadata"]
    input_version = frozen_version(connection, "input", revision["input_id"])
    from bili_asr.storage.import_origins import import_origin, validate_edition_origin
    origin = import_origin(connection, edition_id)
    if version == 2 and input_version == 1:
        baseline = validate_edition_origin(connection, edition, revision)
        from bili_asr.storage.source_supplements import expected_source
        source_matches = normalized["source"] == expected_source(connection, edition, baseline)
    elif version != input_version or origin is not None:
        raise ValueError("publication-integrity: content version or origin differs from frozen input")
    elif version == 2:
        source_matches = normalized["source"]["metadata"] == metadata["source_metadata"]
    else:
        source_matches = (normalized["source"]["bvid"] == metadata["bvid"]
                          and normalized["source"]["pageIndex"] == metadata["page_index"])
    if (normalized != edition["content"] or edition["video_part_id"] != revision["video_part_id"]
            or normalized["source"]["videoPartId"] != edition["video_part_id"]
            or not source_matches):
        raise ValueError("publication-integrity: edition content or source identity mismatch")
    if edition["parent_edition_id"]:
        parent = repository.edition(edition["parent_edition_id"])
        if parent["video_part_id"] != edition["video_part_id"]:
            raise ValueError("publication-integrity: edition parent belongs to another part")
    return edition


def verify_release_identity(connection: sqlite3.Connection, release: dict) -> dict:
    """Read immutable release, approval and audit identity without artifact access."""
    edition = read_edition(connection, release["edition_id"])
    with closing(connection.execute(
        "SELECT * FROM publication_events WHERE release_id = ? ORDER BY event_id",
        (release["release_id"],),
    )) as events:
        validate_release_identity(release, edition, events)
    return edition
