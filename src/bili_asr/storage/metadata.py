"""Metadata implementation."""

from __future__ import annotations

import bili_asr.storage.database as _module_storage_database


from contextlib import contextmanager
import sqlite3
from bili_asr.platform_identity import ContentRef
from bili_asr.source_metadata import SourceMetadataSnapshot
from bili_asr.metadata_policy import MetadataFieldObservation
import json
from uuid import uuid4
from typing import Iterable, Iterator, Mapping
from bili_asr.storage.models import CursorRecord, DiscoveryRecord, IngestionPageRecord, IngestionRunRecord, UserRecord, VideoDetailRecord, VideoPartRecord, VideoRecord, VideoTagRecord, _text


class MetadataRepository:
    """Repository for normalized metadata and ingestion state.

    Commit boundaries per public method:

    - ``start_run`` commits its own insert so a failed page can roll back
      without deleting the run parent.
    - ``finish_run`` commits its own terminal transition.
    - ``record_page`` owns one transaction for its arguments and commits it,
      or rolls it back and re-raises on a write failure; with no payload
      arguments it still commits its own single-write transaction — either
      the ``'failed'`` evidence transaction (page row plus the run's failure
      transition) or the ok/empty/``risk_interrupted`` page-outcome write.
    - ``upsert_user``, ``ensure_user``, ``upsert_video``, ``upsert_part``,
      ``record_discovery`` and ``write_cursor`` execute SQL without
      committing, so a caller can group them in one transaction through
      :meth:`transaction`.
    - ``read_cursor``, ``list_pending_parts`` and ``run_stats`` never write
      or commit.

    Read paths return two shapes: ``read_cursor`` converts its single row
    into a typed ``CursorRecord`` (``None`` when absent), while
    ``list_pending_parts`` and ``run_stats`` return raw ``sqlite3.Row``
    view data — a list for the no-argument form, a single row or ``None``
    for the keyed form. Read-path arguments follow the module-wide
    validation discipline: type errors raise ``TypeError`` and value
    errors raise ``ValueError``.

    Do not compose ``start_run``, ``finish_run`` or ``record_page`` inside a
    :meth:`transaction` group: each commits independently and would commit
    the enclosing group's earlier writes.

    The connection must come with ``row_factory = sqlite3.Row`` and
    ``PRAGMA foreign_keys`` enabled — exactly the state :func:`open_database`
    establishes; the constructor rejects anything else.
    """

    def __init__(self, connection: sqlite3.Connection):
        _module_storage_database._validate_connection(connection)
        self.connection = connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit the enclosed repository operations or roll them back."""
        with _module_storage_database._transaction(self.connection) as connection:
            yield connection

    def upsert_user(self, user: UserRecord) -> None:
        """Insert or update the current display label for a user."""
        if not isinstance(user, UserRecord):
            raise TypeError("user must be a UserRecord")
        self.connection.execute(
            """
            INSERT INTO bilibili_users(mid, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(mid) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (user.mid, user.display_name, user.created_at, user.updated_at),
        )

    def ensure_user(self, user: UserRecord) -> None:
        """Establish a user row only when it does not exist; never rewrite one.

        The run and cursor rows carry a foreign key to ``bilibili_users(mid)``
        (``schema.sql``), so a collection run's opening write must establish the
        parent row before it starts.  It must not *update* one: that write
        happens before any page is fetched, so it has observed nothing to write,
        and an established label may not be replaced by the owner-mid
        placeholder a run-with-no-observation carries.  The placeholder is
        therefore only ever the value a row is *created* with.

        :meth:`upsert_user` stays the refreshing write: it is how a name the
        run did observe reaches an existing row, and it overwrites.
        """
        if not isinstance(user, UserRecord):
            raise TypeError("user must be a UserRecord")
        self.connection.execute(
            """
            INSERT INTO bilibili_users(mid, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(mid) DO NOTHING
            """,
            (user.mid, user.display_name, user.created_at, user.updated_at),
        )

    def upsert_video(self, video: VideoRecord, *, refresh_pubdate: bool = False) -> None:
        """Insert or update a video's current canonical display fields.

        The stored ``aid`` is a stable identifier: the first non-``None``
        ``aid`` wins — a stored ``NULL`` is backfilled from the incoming
        record, and a known ``aid`` is never overwritten.
        """
        if not isinstance(video, VideoRecord):
            raise TypeError("video must be a VideoRecord")
        prior = self.connection.execute("SELECT title,pubdate,aid FROM videos WHERE bvid=?", (video.bvid,)).fetchone()
        self.connection.execute(
            """
            INSERT INTO videos(
                bvid, aid, mid, title, pubdate, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid) DO UPDATE SET
                aid = COALESCE(videos.aid, excluded.aid),
                title = excluded.title,
                pubdate = CASE WHEN excluded.pubdate > 0 AND (videos.pubdate = 0 OR ?)
                               THEN excluded.pubdate ELSE videos.pubdate END,
                updated_at = excluded.updated_at
            """,
            (
                video.bvid,
                video.aid,
                video.mid,
                video.title,
                video.pubdate,
                video.created_at,
                video.updated_at,
                int(refresh_pubdate),
            ),
        )
        if prior is not None:
            current = self.connection.execute("SELECT title,pubdate,aid FROM videos WHERE bvid=?", (video.bvid,)).fetchone()
            from bili_asr.storage.metadata_acceptance import invalidate_fields
            invalidate_fields(self.connection, video.bvid, tuple(name for index, name in enumerate(("title", "pubdate", "aid"))
                              if prior[index] != current[index]))

    def read_source_metadata(self, video_part_id: int) -> SourceMetadataSnapshot:
        """Read one frozen-input candidate without modifying the archive."""
        if self.observations_supported():
            return self._read_source_metadata_batch((video_part_id,))[0][video_part_id]
        row = self.connection.execute(
            """SELECT p.bvid, p.page_index, p.title AS part_title, p.duration_ms,
                      v.title, v.mid, v.aid, v.pubdate, v.updated_at,
                      u.display_name, d.pic, d."desc", d.tid
               FROM video_parts p JOIN videos v ON v.bvid=p.bvid
               JOIN bilibili_users u ON u.mid=v.mid
               LEFT JOIN video_details d ON d.bvid=v.bvid
               WHERE p.video_part_id=?""", (video_part_id,),
        ).fetchone()
        if row is None:
            raise ValueError("source metadata part is not archived")
        tags = tuple(dict.fromkeys(item[0] for item in self.connection.execute(
            "SELECT tag_name FROM video_tags WHERE bvid=? ORDER BY tag_id", (row["bvid"],))))
        return SourceMetadataSnapshot(
            ContentRef("bilibili", row["bvid"], row["page_index"]), row["title"],
            str(row["mid"]), row["display_name"], row["pubdate"], row["updated_at"],
            row["desc"], row["pic"], row["tid"], tags, row["aid"], row["part_title"], row["duration_ms"],
        )

    def read_source_metadata_many(
        self, video_part_ids: Iterable[int],
    ) -> dict[int, SourceMetadataSnapshot]:
        """Read frozen-input candidates with bounded set queries."""
        return self._read_source_metadata_batch(video_part_ids)[0]

    def read_source_metadata_export_many(
        self, video_part_ids: Iterable[int], *, include_source_metadata: bool = True,
    ) -> dict[int, dict[str, object]]:
        """Read current source facts, with field-specific collection semantics.

        The flat fields are export projections, not additions to the frozen
        source-metadata-v1 codec. In particular details collection time does
        not claim that uploader names or retained tags were observed then.
        """
        snapshots, fields = self._read_source_metadata_batch(
            video_part_ids, include_source_metadata=include_source_metadata, include_export_facts=True,
        )
        return {part_id: {**values, **({"sourceMetadata": snapshots[part_id].to_dict()}
                                     if include_source_metadata else {})}
                for part_id, values in fields.items()}

    def _read_source_metadata_batch(
        self, video_part_ids: Iterable[int], *, include_source_metadata: bool = True,
        include_export_facts: bool = False,
    ) -> tuple[dict[int, SourceMetadataSnapshot], dict[int, dict[str, object]]]:
        """Read at most 256 parts with set queries and no archive mutation.

        The caller owns any encompassing read snapshot. Bilibili tags and
        details retain their provider facts, while another platform never
        acquires synthetic Bilibili identifiers or metadata. Missing IDs fail
        the entire read rather than returning a silently incomplete mapping.
        """
        part_ids: list[int] = []
        seen: set[int] = set()
        for offset, part_id in enumerate(video_part_ids):
            if offset >= 256:
                raise ValueError("source metadata read is limited to 256 part IDs")
            if type(part_id) is not int or part_id < 1:
                raise ValueError("source metadata part IDs must be positive integers")
            if part_id not in seen:
                seen.add(part_id)
                part_ids.append(part_id)
        if not part_ids:
            return {}, {}
        marks = ",".join("?" for _ in part_ids)
        supports_observations = self.observations_supported()
        if supports_observations:
            query = f"""SELECT s.video_part_id,s.platform,s.external_video_id,s.page_index,
                        s.title AS part_title,s.duration_ms,
                        CASE WHEN s.platform='bilibili' THEN v.title ELSE s.video_title END AS title,
                        CASE WHEN s.platform='bilibili' THEN CAST(v.mid AS TEXT)
                             ELSE s.creator_external_id END AS creator_id,
                        CASE WHEN s.platform='bilibili' THEN u.display_name
                             ELSE s.creator_name END AS creator_name,
                        CASE WHEN s.platform='bilibili' THEN v.pubdate ELSE s.pubdate END AS pubdate,
                        CASE WHEN s.platform='bilibili' THEN v.updated_at ELSE s.observed_at END AS observed_at,
                        d.pic,d."desc",d.tid,v.aid,v.mid,d.observed_at AS details_observed_at
                 FROM v_source_parts s
                 LEFT JOIN videos v ON s.platform='bilibili' AND v.bvid=s.external_video_id
                 LEFT JOIN bilibili_users u ON u.mid=v.mid
                 LEFT JOIN video_details d ON d.bvid=v.bvid
                 WHERE s.video_part_id IN ({marks})"""
        else:
            query = f"""SELECT p.video_part_id,'bilibili' AS platform,
                        p.bvid AS external_video_id,p.page_index,p.title AS part_title,p.duration_ms,
                        v.title,CAST(v.mid AS TEXT) AS creator_id,u.display_name AS creator_name,
                        v.pubdate,v.updated_at AS observed_at,d.pic,d."desc",d.tid,v.aid,
                        v.mid,d.observed_at AS details_observed_at
                 FROM video_parts p JOIN videos v ON v.bvid=p.bvid
                 JOIN bilibili_users u ON u.mid=v.mid
                 LEFT JOIN video_details d ON d.bvid=v.bvid
                 WHERE p.video_part_id IN ({marks})"""
        rows = self.connection.execute(query, part_ids).fetchall()
        if {int(row["video_part_id"]) for row in rows} != seen:
            raise ValueError("source metadata part is not archived")
        bvids = tuple(dict.fromkeys(row["external_video_id"] for row in rows if row["platform"] == "bilibili"))
        tags: dict[str, list[str]] = {}
        tag_states: dict[str, str] = {}
        if bvids:
            tag_marks = ",".join("?" for _ in bvids)
            for tag in self.connection.execute(
                f"SELECT bvid,tag_name FROM video_tags WHERE bvid IN ({tag_marks}) ORDER BY bvid,tag_id", bvids,
            ):
                values = tags.setdefault(tag["bvid"], [])
                if tag["tag_name"] not in values:
                    values.append(tag["tag_name"])
            # Old runtime databases may predate observation storage. Missing
            # evidence means unknown even when retained tag values exist.
            has_tag_observations = include_export_facts and self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_tag_observations'"
            ).fetchone() is not None
            if has_tag_observations:
                tag_states = {row["bvid"]: row["state"] for row in self.connection.execute(
                    f"SELECT bvid,state FROM video_tag_observations WHERE bvid IN ({tag_marks})", bvids,
                )}
        from bili_asr.storage.source_projection import read_youtube_fields_many
        youtube_ids = tuple(dict.fromkeys(row["external_video_id"] for row in rows if row["platform"] == "youtube"))
        youtube = read_youtube_fields_many(self.connection, youtube_ids) if youtube_ids else {}
        from bili_asr.storage.metadata_acceptance import read_bindings_many
        bilibili = read_bindings_many(self.connection, bvids) if bvids and supports_observations else {}
        snapshots = {
            int(row["video_part_id"]): SourceMetadataSnapshot(
                ContentRef(row["platform"], row["external_video_id"], row["page_index"]),
                row["title"], row["creator_id"], row["creator_name"], row["pubdate"], row["observed_at"],
                row["desc"] if row["platform"] == "bilibili" else youtube[row["external_video_id"]].description,
                row["pic"] if row["platform"] == "bilibili" else youtube[row["external_video_id"]].cover_url,
                row["tid"], tuple(tags.get(row["external_video_id"], ()))
                if row["platform"] == "bilibili" else youtube[row["external_video_id"]].tags,
                row["aid"], row["part_title"], row["duration_ms"],
                2 if (bool(bilibili.get(row["external_video_id"])) if row["platform"] == "bilibili"
                      else bool(youtube[row["external_video_id"]].observed_fields)) else 1,
                bilibili.get(row["external_video_id"], ()) if row["platform"] == "bilibili"
                    else youtube[row["external_video_id"]].bindings,
            ) for row in rows
        } if include_source_metadata else {}
        fields = {
            int(row["video_part_id"]): {
                "video_description": row["desc"] if row["platform"] == "bilibili" else youtube[row["external_video_id"]].description,
                "uploader_mid": row["mid"] if row["platform"] == "bilibili" else None,
                "uploader_name": row["creator_name"],
                "video_tags": list(tags.get(row["external_video_id"], ()))
                    if row["platform"] == "bilibili" else list(youtube[row["external_video_id"]].tags),
                "video_tags_state": tag_states.get(row["external_video_id"])
                    if row["platform"] == "bilibili" else {"present": "success_nonempty", "empty": "success_empty",
                         "missing": "unknown", "unavailable": "unavailable", "denied": "denied"}.get(youtube[row["external_video_id"]].tag_state),
                "video_details_observed_at": row["details_observed_at"] if row["platform"] == "bilibili"
                    else youtube[row["external_video_id"]].details_observed_at,
            } for row in rows
        }
        return ({part_id: snapshots[part_id] for part_id in part_ids}
                if include_source_metadata else {}), fields

    def upsert_part(self, part: VideoPartRecord) -> int:
        """Insert or update a normalized part and return its local ID.

        ``video_part_id`` is allocated by the repository, so the record must
        carry ``video_part_id=None``; a non-``None`` id raises ``ValueError``.
        Conflicts on ``(bvid, page_index)`` update only the current display
        fields and non-key facts.
        """
        if not isinstance(part, VideoPartRecord):
            raise TypeError("part must be a VideoPartRecord")
        if part.video_part_id is not None:
            raise ValueError("upsert_part allocates video_part_id; it must be None")
        current = self.connection.execute(
            "SELECT cid,title,duration_ms FROM video_parts WHERE bvid=? AND page_index=?", (part.bvid, part.page_index),
        ).fetchone()
        if current is not None and current[0] != part.cid:
            raise ValueError("metadata_part_identity_conflict")
        if current is None or current[1] != part.title or current[2] != part.duration_ms:
            from bili_asr.storage.metadata_acceptance import invalidate_fields
            invalidate_fields(self.connection, part.bvid, ("parts",))
        self.connection.execute(
            """
            INSERT INTO video_parts(
                bvid, page_index, cid, title, duration_ms, processing_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid, page_index) DO UPDATE SET
                title = excluded.title,
                duration_ms = excluded.duration_ms,
                processing_status = excluded.processing_status,
                updated_at = excluded.updated_at
            """,
            (
                part.bvid,
                part.page_index,
                part.cid,
                part.title,
                part.duration_ms,
                part.processing_status,
                part.created_at,
                part.updated_at,
            ),
        )

        row = self.connection.execute(
            """
            SELECT video_part_id
            FROM video_parts
            WHERE bvid = ? AND page_index = ?
            """,
            (part.bvid, part.page_index),
        ).fetchone()
        if row is None:  # pragma: no cover - the preceding INSERT guarantees this
            raise sqlite3.DatabaseError("upserted video part could not be read back")
        return int(row[0])

    def upsert_video_details(self, details: VideoDetailRecord) -> None:
        """Insert or refresh one video's category and cover observation.

        **One row per video, and it is refreshed** (compass D11): a second
        collection of the same ``bvid`` replaces the row rather than adding
        one, so ``SELECT COUNT(*)`` for that video stays ``1`` forever and
        ``observed_at`` always holds the newest observation's stamp.  A reader
        must not treat this table as a history: it cannot answer "what did
        upstream say on 2026-09-26", because nothing here is dated beyond the
        single row's own last-write stamp.

        **``observed_at`` means "last *successful* collection" — the guard is
        the condition, not an adjective** (compass D15).  All three value
        columns are nullable, so an unconditional ``ON CONFLICT ... DO UPDATE
        SET`` could blank a populated row with ``NULL``s and stamp it fresh,
        recording "nothing was true at T" where the collection established no
        such thing.  The write therefore happens **only when the incoming
        observation carries at least one of ``pic``/``desc``/``tid``**: an
        all-``NULL`` observation leaves the existing row and its ``observed_at``
        untouched, and writes no row at all for a video that has none.  The
        grain is unchanged — one row per video, refreshed — so the guard
        constrains *when* the stamp moves, not what the table holds.

        **A partial observation refreshes the whole row, the stamp included.**
        "All three are ``None``" is the whole of the skip condition, so an
        observation carrying only one of the three is a successful collection:
        the row is written to exactly what it carried, the unobserved columns go
        to ``NULL``, and ``observed_at`` advances with them.  That is D11 applied
        verbatim — metadata "is refreshed on recollect … a later collection
        overwrites it" — because the row is the last collection's *view* of the
        video, not a per-column last-known-good, so a value upstream really did
        drop does not survive as a stale one.  Per-column ``COALESCE`` would keep
        a genuinely retracted cover alive, which is the same class of fiction
        D15 exists to prevent; a partial observation establishes exactly that
        much and nothing here is per-column.

        ``"desc"`` is quoted because ``desc`` is a SQL keyword and the column
        keeps upstream's own field name.  ``pic`` holds the cover URL in the
        store; ``export`` still redacts its value and this is intended and
        permanent for this iteration (compass D12 — the cover is store-only,
        and a cover that must appear in an export is a new decision).
        """

        if not isinstance(details, VideoDetailRecord):
            raise TypeError("details must be a VideoDetailRecord")
        if (
            details.pic is None
            and details.desc is None
            and details.tid is None
        ):
            # Nothing was observed: leave the row and its stamp alone, and do
            # not create one for a video that has none (D15).
            return
        prior = self.connection.execute('SELECT pic,"desc",tid FROM video_details WHERE bvid=?', (details.bvid,)).fetchone()
        if prior is not None:
            from bili_asr.storage.metadata_acceptance import invalidate_fields
            invalidate_fields(self.connection, details.bvid, tuple(name for index, name in enumerate(("pic", "desc", "tid"))
                              if prior[index] != getattr(details, name)))
        self.connection.execute(
            """
            INSERT INTO video_details(bvid, pic, "desc", tid, observed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(bvid) DO UPDATE SET
                pic = excluded.pic,
                "desc" = excluded."desc",
                tid = excluded.tid,
                observed_at = excluded.observed_at
            """,
            (
                details.bvid,
                details.pic,
                details.desc,
                details.tid,
                details.observed_at,
            ),
        )

    def observe_video_details(self, details: VideoDetailRecord,
                              fields: tuple[MetadataFieldObservation, ...] | None = None) -> None:
        """Update only fields actually observed; explicit empty can retract data.

        The older whole-row method remains available for callers possessing a
        complete snapshot. Partial list/detail observations use this boundary.
        """
        fields = fields if fields is not None else tuple(
            MetadataFieldObservation(name, "present", getattr(details, name))
            for name in ("pic", "desc", "tid") if getattr(details, name) is not None)
        known = [field for field in fields if field.field in {"pic", "desc", "tid"}
                 and field.state in {"present", "empty"}]
        if not known:
            return
        self.connection.execute("INSERT INTO video_details(bvid, observed_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                                (details.bvid, details.observed_at))
        for field in known:
            prior = self.connection.execute(f'SELECT "{field.field}" FROM video_details WHERE bvid=?', (details.bvid,)).fetchone()
            if prior[0] != field.value:
                from bili_asr.storage.metadata_acceptance import invalidate_fields
                invalidate_fields(self.connection, details.bvid, (field.field,))
            # Column names come from the closed field vocabulary above.
            self.connection.execute(f'UPDATE video_details SET "{field.field}"=?, observed_at=? WHERE bvid=?',
                                    (field.value, details.observed_at, details.bvid))

    def observations_supported(self) -> bool:
        from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2
        return runtime_contract(self.connection) == UNIVERSAL_V2

    def record_metadata_attempt(self, bvid: str, operation: str, state: str, started_at: int,
                                finished_at: int, error_code: str | None = None,
                                *, details: Mapping | None = None) -> None:
        if not self.observations_supported():
            return
        from bili_asr.workflow_errors import safe_job_details
        diagnostic = safe_job_details({**(details or {}), "operation": operation, "error_code": error_code})
        self.connection.execute(
            "INSERT INTO metadata_refresh_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, bvid, operation, state, error_code, started_at, finished_at,
             json.dumps(diagnostic, sort_keys=True)),
        )

    def record_field_observations(self, bvid: str, operation: str,
                                  fields: tuple[MetadataFieldObservation, ...], observed_at: int) -> None:
        if not self.observations_supported():
            return
        for field in fields:
            successful = field.state in {"present", "empty"}
            value_json = json.dumps(field.value, ensure_ascii=False) if successful else None
            success_at = observed_at if successful else None
            if successful:
                from bili_asr.metadata_acceptance import unpack_cache
                prior = self.connection.execute("SELECT value_json,last_success_at FROM source_metadata_observations "
                    "WHERE platform='bilibili' AND external_id=? AND operation=? AND field=?", (bvid, operation, field.field)).fetchone()
                # A repeat value with no new gateway proof retains its genuine
                # adoption; a rejected stable-ID/publication update cannot
                # replace the retained current field's successful evidence.
                table = "videos" if operation == "summary" else "video_details"
                current = self.connection.execute(f'SELECT "{field.field}" FROM {table} WHERE bvid=?', (bvid,)).fetchone()
                if current is not None and current[0] != field.value:
                    value_json, success_at = None, None
                elif prior is not None:
                    prior_value, cache = unpack_cache(prior[0])
                    if cache is not None and prior_value == field.value:
                        value_json, success_at = prior[0], prior[1]
            self.connection.execute(
                """INSERT INTO source_metadata_observations VALUES ('bilibili', ?, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(platform, external_id, operation, field) DO UPDATE SET
                   state=excluded.state, observed_at=excluded.observed_at, error_code=NULL,
                   last_success_at=CASE WHEN excluded.last_success_at IS NULL
                        THEN source_metadata_observations.last_success_at ELSE excluded.last_success_at END,
                   value_json=CASE WHEN excluded.last_success_at IS NULL
                        THEN source_metadata_observations.value_json ELSE excluded.value_json END""",
                (bvid, operation, field.field, field.state, observed_at,
                 success_at, value_json),
            )

    def record_accepted_receipts(self, bvid: str, receipts, observed_at: int) -> None:
        if self.observations_supported():
            from bili_asr.storage.metadata_acceptance import record_acceptance
            record_acceptance(self.connection, bvid, receipts, observed_at)

    def upsert_video_tags(
        self, bvid: str, tags: Iterable[VideoTagRecord] = ()
    ) -> None:
        """Replace one video's tag set with the observed one.

        The tag set is a *set of facts about a video*, not an append-only
        log: recollecting a video whose tags changed must converge on what
        upstream says now rather than accumulate both answers.  The video's
        existing rows are therefore deleted and the observed set inserted, in
        one statement pair — inside the caller's transaction, so the
        replacement commits or rolls back with the rest of that page's
        payload.  ``bvid`` carries no tags is how a set is cleared.

        Order is not significant: the tag identity is ``(bvid, tag_id)``, so
        the same set converges regardless of the order upstream listed it in.
        A record whose ``bvid`` differs from the argument is refused rather
        than written under another video's key.
        """

        _text(bvid, "bvid")
        tag_records = tuple(tags)
        for tag in tag_records:
            if not isinstance(tag, VideoTagRecord):
                raise TypeError("tags must be VideoTagRecord instances")
            if tag.bvid != bvid:
                raise ValueError("every tag record must carry the given bvid")
        from itertools import zip_longest
        prior = self.connection.execute("SELECT tag_id,tag_name,tag_type FROM video_tags WHERE bvid=? ORDER BY tag_id", (bvid,))
        expected = sorted((tag.tag_id, tag.tag_name, tag.tag_type) for tag in tag_records)
        if any(stored != incoming for stored, incoming in zip_longest((tuple(row) for row in prior), expected)):
            from bili_asr.storage.metadata_acceptance import invalidate_fields
            invalidate_fields(self.connection, bvid, ("tags",))
        self.connection.execute("DELETE FROM video_tags WHERE bvid = ?", (bvid,))
        self.connection.executemany(
            """
            INSERT INTO video_tags(bvid, tag_id, tag_name, tag_type)
            VALUES (?, ?, ?, ?)
            """,
            [(tag.bvid, tag.tag_id, tag.tag_name, tag.tag_type) for tag in tag_records],
        )

    def start_run(self, run: IngestionRunRecord) -> None:
        """Insert one new run record.

        ``run_id`` is the primary key and is never reused: a duplicate raises
        ``sqlite3.IntegrityError``.
        """
        if not isinstance(run, IngestionRunRecord):
            raise TypeError("run must be an IngestionRunRecord")
        self.connection.execute(
            """
            INSERT INTO ingestion_runs(
                run_id, mid, source_package, source_version, requested_start_page,
                requested_page_limit, started_at, finished_at, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.mid,
                run.source_package,
                run.source_version,
                run.requested_start_page,
                run.requested_page_limit,
                run.started_at,
                run.finished_at,
                run.outcome,
            ),
        )
        # A run is a lifecycle parent for page transactions. Commit its start
        # independently so a failed page can roll back without deleting it.
        self.connection.commit()

    def finish_run(self, run: IngestionRunRecord) -> None:
        """Finish a run with its terminal outcome and finish timestamp.

        The canonical form is ``finish_run(IngestionRunRecord(...))`` with a
        terminal ``outcome`` and an integer ``finished_at``. The ordering
        baseline is the run's stored ``started_at``, not the record's own
        ``started_at`` field. Re-finishing is rejected: a run whose stored
        outcome is already terminal raises ``sqlite3.IntegrityError``.
        """
        if not isinstance(run, IngestionRunRecord):
            raise TypeError("run must be an IngestionRunRecord")
        run_row = self.connection.execute(
            "SELECT started_at, outcome FROM ingestion_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        if run_row is None:
            raise sqlite3.IntegrityError(f"unknown run_id: {run.run_id}")
        if run_row["outcome"] != "running":
            raise sqlite3.IntegrityError(
                f"run {run.run_id} already finished with outcome {run_row['outcome']}"
            )
        if run.outcome not in _module_storage_database._TERMINAL_RUN_OUTCOMES:
            raise ValueError("finish_run requires a terminal run outcome")
        if run.finished_at is None:
            raise ValueError("finished_at is required when finishing a run")
        started_at = int(run_row["started_at"])
        if run.finished_at < started_at:
            raise ValueError("finished_at must not precede started_at")
        self.connection.execute(
            "UPDATE ingestion_runs SET finished_at = ?, outcome = ? WHERE run_id = ?",
            (run.finished_at, run.outcome, run.run_id),
        )
        if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise sqlite3.IntegrityError(f"unknown run_id: {run.run_id}")
        self.connection.commit()

    def _record_page(self, page: IngestionPageRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO ingestion_pages(
                run_id, page_number, outcome, error_code, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, page_number) DO UPDATE SET
                outcome = excluded.outcome,
                error_code = excluded.error_code,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at
            """,
            (
                page.run_id,
                page.page_number,
                page.outcome,
                page.error_code,
                page.started_at,
                page.finished_at,
            ),
        )

    def record_page(
        self,
        page: IngestionPageRecord,
        user: UserRecord | None = None,
        videos: Iterable[VideoRecord] = (),
        parts: Iterable[VideoPartRecord] = (),
        discoveries: Iterable[DiscoveryRecord] = (),
        cursor: CursorRecord | None = None,
        tags: Mapping[str, Iterable[VideoTagRecord]] | None = None,
        details: Iterable[VideoDetailRecord] = (),
        additional_users: Iterable[UserRecord] = (),
        ensure_users: Iterable[UserRecord] = (),
        tag_observations: Mapping[str, tuple[str, str | None]] | None = None,
        preserve_unobserved_details: bool = False,
        accepted_receipts: Mapping | None = None,
    ) -> None:
        """Record one page outcome, optionally with its complete payload.

        With payload arguments the method owns one transaction and applies the
        locked parent-before-child order: user, videos, parts, tag sets,
        details, discoveries, cursor, page outcome, commit. If any write fails,
        the whole transaction is rolled back and the exception is re-raised;
        the prior cursor and entities are unchanged. Recording the resulting
        failure is the caller's step: build a fresh ``IngestionPageRecord`` with
        ``outcome='failed'`` and a bounded ``error_code`` and call this method
        again with no payload arguments.

        ``additional_users`` contains observed collaborating uploader names.
        ``ensure_users`` establishes unnamed uploaders without overwriting
        existing names. Both are written before videos in the same transaction.

        A ``'failed'`` page therefore never carries payloads — supplying
        payload arguments with ``outcome='failed'`` raises ``ValueError``
        before any write. A no-payload ``'failed'`` page is recorded in its
        own committed transaction together with the parent run's failure
        transition. When that run is already terminal, the page evidence is
        still persisted while the run's outcome and ``finished_at`` stay
        unchanged. A no-payload page with a non-failed outcome (``ok``,
        ``empty``, ``risk_interrupted``) likewise commits its own
        single-write transaction for the page-outcome row.

        The failure transition applies only while the run is still
        ``running`` and uses the run's stored ``started_at`` as its ordering
        baseline — the same DB baseline as :meth:`finish_run`: a failed page
        whose ``finished_at`` precedes the run's stored ``started_at`` is
        rejected with ``ValueError`` and nothing is persisted.
        """
        if not isinstance(page, IngestionPageRecord):
            raise TypeError("page must be an IngestionPageRecord")
        video_records = tuple(videos)
        part_records = tuple(parts)
        discovery_records = tuple(discoveries)
        detail_records = tuple(details)
        additional_user_records = tuple(additional_users)
        ensured_user_records = tuple(ensure_users)
        # ``tags`` is a mapping rather than a flat iterable because the
        # replacement is per video: ``None`` means "this page observed no tag
        # sets at all" (a run whose tag calls all degraded, or a page whose
        # videos were already recorded), while a key present with an empty
        # iterable means "this video was observed to carry no tags" and clears
        # its rows.  The two are deliberately different: conflating them would
        # turn a failed tag fetch into a silent erasure of known tags.
        tag_sets = None if tags is None else dict(tags)
        has_payload = (
            user is not None
            or bool(additional_user_records)
            or bool(ensured_user_records)
            or bool(video_records)
            or bool(part_records)
            or bool(discovery_records)
            or bool(detail_records)
            or cursor is not None
            or tag_sets is not None
            or bool(tag_observations)
            or bool(accepted_receipts)
        )
        if page.outcome == "failed" and has_payload:
            raise ValueError("a failed page is recorded without payload arguments")

        if not has_payload:
            if page.outcome == "failed":
                self._record_failed_page(page)
            else:
                with self.transaction():
                    self._record_page(page)
            return

        with self.transaction():
            observations = self.observations_supported()
            for owner in ensured_user_records:
                self.ensure_user(owner)
            if user is not None:
                self.upsert_user(user)
            for owner in additional_user_records:
                self.upsert_user(owner)
            for video_record in video_records:
                self.upsert_video(video_record)
                if observations:
                    fields = tuple(MetadataFieldObservation(name, "present" if value is not None else "missing", value)
                                   for name, value in (("title", video_record.title), ("pubdate", video_record.pubdate), ("aid", video_record.aid)))
                    self.record_field_observations(video_record.bvid, "summary", fields, page.finished_at)
                    self.record_metadata_attempt(video_record.bvid, "summary", "present", page.started_at, page.finished_at)
            for part in part_records:
                self.upsert_part(part)
            if observations:
                for bvid in {part.bvid for part in part_records}:
                    self.record_metadata_attempt(bvid, "parts", "present", page.started_at, page.finished_at)
            # Tags land after the video upserts: the tag row's foreign key
            # points at ``videos``, so an observed video must exist before its
            # tags can.  A tag set for a video this page did not upsert still
            # writes here — the FK then decides, rather than this method
            # silently dropping the observation.
            for tag_bvid, tag_records in (tag_sets or {}).items():
                self.upsert_video_tags(tag_bvid, tag_records)
            for bvid, (state, error_code) in (tag_observations or {}).items():
                self.connection.execute(
                    "INSERT INTO video_tag_observations VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(bvid) DO UPDATE SET state=excluded.state, observed_at=excluded.observed_at, "
                    "error_code=excluded.error_code, run_id=excluded.run_id",
                    (bvid, state, page.finished_at, error_code, page.run_id),
                )
                if observations:
                    self.record_metadata_attempt(bvid, "tags", "unavailable" if state == "unavailable"
                                                 else "empty" if state == "success_empty" else "present",
                                                 page.started_at, page.finished_at, error_code)
            # Details land after the same video upserts, for the same foreign
            # key reason.  An all-``NULL`` record is passed through rather than
            # filtered here: ``upsert_video_details`` is where D15's
            # "observed nothing" rule lives, so it stays one rule in one place.
            for detail in detail_records:
                if preserve_unobserved_details:
                    self.observe_video_details(detail)
                else:
                    self.upsert_video_details(detail)
                if observations:
                    fields = tuple(MetadataFieldObservation(name, "present" if value is not None else "missing", value)
                                   for name, value in (("pic", detail.pic), ("desc", detail.desc), ("tid", detail.tid)))
                    self.record_field_observations(detail.bvid, "details", fields, page.finished_at)
                    self.record_metadata_attempt(detail.bvid, "details", "present" if any(
                        field.state == "present" for field in fields) else "missing", page.started_at, page.finished_at)
            for bvid, receipts in (accepted_receipts or {}).items():
                self.record_accepted_receipts(bvid, receipts, page.finished_at)
            for discovery in discovery_records:
                self.record_discovery(discovery)
            if cursor is not None:
                self.write_cursor(cursor)
            self._record_page(page)

    def _record_failed_page(self, page: IngestionPageRecord) -> None:
        """Persist only bounded failure state after a rolled-back page.

        The page evidence is upserted in the same committed transaction as
        the parent run's failure transition, which applies only while the
        run is still ``'running'`` — a late or stale failed page can never
        regress a terminal outcome or move ``finished_at`` backwards. The
        transition validates against the run's stored ``started_at`` (the
        same DB baseline as :meth:`finish_run`): a page clock below the
        run's start raises ``ValueError`` and nothing is persisted.
        """
        with self.transaction():
            run_row = self.connection.execute(
                "SELECT started_at, outcome FROM ingestion_runs WHERE run_id = ?",
                (page.run_id,),
            ).fetchone()
            if (
                run_row is not None
                and run_row["outcome"] == "running"
                and page.finished_at < int(run_row["started_at"])
            ):
                raise ValueError("finished_at must not precede started_at")
            self._record_page(page)
            self.connection.execute(
                """
                UPDATE ingestion_runs
                SET outcome = 'failed', finished_at = ?
                WHERE run_id = ? AND outcome = 'running'
                """,
                (page.finished_at, page.run_id),
            )

    def record_discovery(self, discovery: DiscoveryRecord) -> None:
        """Insert or update one run/page/video discovery relationship."""
        if not isinstance(discovery, DiscoveryRecord):
            raise TypeError("discovery must be a DiscoveryRecord")
        self.connection.execute(
            """
            INSERT INTO ingestion_discoveries(
                run_id, page_number, bvid, source_position, discovered_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, page_number, bvid) DO UPDATE SET
                source_position = excluded.source_position,
                discovered_at = excluded.discovered_at
            """,
            (
                discovery.run_id,
                discovery.page_number,
                discovery.bvid,
                discovery.source_position,
                discovery.discovered_at,
            ),
        )

    def read_resume_prefix(self, mid: int, next_page: int) -> dict[int, tuple[str, ...]]:
        """Read the most recent successful identities for each preceding page.

        Missing pages remain missing: a skipped or unobserved prefix cannot
        establish a safe offset. Discovery order uses the stored last position
        of duplicate BVIDs, matching record_page's existing identity contract.
        """
        for name, value in (("mid", mid), ("next_page", next_page)):
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        rows = self.connection.execute(
            """
            WITH latest AS (
                SELECT p.run_id, p.page_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY p.page_number
                           ORDER BY p.finished_at DESC, p.rowid DESC
                       ) AS rank
                FROM ingestion_pages p JOIN ingestion_runs r USING (run_id)
                WHERE r.mid = ? AND p.page_number < ? AND p.outcome = 'ok'
            )
            SELECT l.page_number, d.bvid FROM latest l
            JOIN ingestion_discoveries d
              ON d.run_id = l.run_id AND d.page_number = l.page_number
            WHERE l.rank = 1
            ORDER BY l.page_number, d.source_position
            """, (mid, next_page),
        ).fetchall()
        pages: dict[int, list[str]] = {}
        for row in rows:
            pages.setdefault(row["page_number"], []).append(row["bvid"])
        return {number: tuple(bvids) for number, bvids in pages.items()}

    def read_cursor(self, mid: int) -> CursorRecord | None:
        """Read the current one-based cursor for a user.

        Returns a typed ``CursorRecord`` or ``None`` when the user has no
        cursor row.
        """
        if isinstance(mid, bool) or not isinstance(mid, int):
            raise TypeError("mid must be an integer")
        if mid < 1:
            raise ValueError("mid must be a positive integer")
        row = self.connection.execute(
            """
            SELECT mid, next_page, observed_total, state, last_error_code, updated_at
            FROM ingestion_cursors
            WHERE mid = ?
            """,
            (mid,),
        ).fetchone()
        if row is None:
            return None
        return CursorRecord(
            mid=int(row["mid"]),
            next_page=int(row["next_page"]),
            observed_total=(
                None if row["observed_total"] is None else int(row["observed_total"])
            ),
            state=str(row["state"]),
            last_error_code=(
                None if row["last_error_code"] is None else str(row["last_error_code"])
            ),
            updated_at=int(row["updated_at"]),
        )

    def write_cursor(self, cursor: CursorRecord) -> None:
        """Insert or replace the resumable cursor for a user."""
        if not isinstance(cursor, CursorRecord):
            raise TypeError("cursor must be a CursorRecord")
        self.connection.execute(
            """
            INSERT INTO ingestion_cursors(
                mid, next_page, observed_total, state, last_error_code, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mid) DO UPDATE SET
                next_page = excluded.next_page,
                observed_total = excluded.observed_total,
                state = excluded.state,
                last_error_code = excluded.last_error_code,
                updated_at = excluded.updated_at
            """,
            (
                cursor.mid,
                cursor.next_page,
                cursor.observed_total,
                cursor.state,
                cursor.last_error_code,
                cursor.updated_at,
            ),
        )

    def list_pending_parts(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Return discovered parts in deterministic work order.

        Rows come straight from the ``v_pending_metadata`` view.
        """
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("limit must be an integer or None")
            if limit < 1:
                raise ValueError("limit must be a positive integer")
            query = (
                "SELECT * FROM v_pending_metadata "
                "ORDER BY bvid, page_index LIMIT ?"
            )
            return list(self.connection.execute(query, (limit,)).fetchall())
        return list(
            self.connection.execute(
                "SELECT * FROM v_pending_metadata ORDER BY bvid, page_index"
            ).fetchall()
        )

    def run_stats(self, run_id: str | None = None) -> sqlite3.Row | list[sqlite3.Row] | None:
        """Read normalized run/page/video counts from the repository view.

        Returns one ``v_ingestion_run_stats`` row for a ``run_id``, or a
        list of every run's rows when ``run_id`` is ``None``.
        """
        if run_id is None:
            return list(
                self.connection.execute(
                    "SELECT * FROM v_ingestion_run_stats ORDER BY run_id"
                ).fetchall()
            )
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string or None")
        if not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        return self.connection.execute(
            "SELECT * FROM v_ingestion_run_stats WHERE run_id = ?", (run_id,)
        ).fetchone()
