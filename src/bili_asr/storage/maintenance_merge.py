"""Lossless relational union. Conflicting identities never overwrite history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from bili_asr.storage.maintenance_selection import _JSON_EDGES
from bili_asr.storage.snapshots import _identifier as quote
from bili_asr.storage.snapshots import _schema, validate_snapshot_database
from bili_asr.storage.upgrade_evidence import readonly

# These triggers mirror legacy Bilibili provider rows into the neutral source
# projection. They must not fire while a complete archive is being copied:
# source projection rows are copied later in the same merge and would otherwise
# collide on their natural keys. Identity and immutable-history guards are
# intentionally excluded and remain enabled during the merge.
_PROJECTION_TRIGGERS = (
    "source_creator_from_bili_insert",
    "source_creator_from_bili_update",
    "source_video_from_bili_insert",
    "source_video_from_bili_update",
    "source_part_from_bili_insert",
)


def _projection_trigger_inventory(connection: sqlite3.Connection) -> tuple[dict[str, str], str]:
    """Return the exact projection trigger SQL and a stable inventory hash."""
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?,?,?) ORDER BY name",
        _PROJECTION_TRIGGERS,
    ).fetchall()
    inventory = {name: sql for name, sql in rows}
    present = set(inventory)
    if present and present != set(_PROJECTION_TRIGGERS) or any(not sql for sql in inventory.values()):
        missing = sorted(set(_PROJECTION_TRIGGERS) - set(inventory))
        raise ValueError("merge requires the complete source projection trigger contract" +
                         ((": " + ",".join(missing)) if missing else ""))
    canonical = "\n".join(f"{name}\0{inventory[name]}" for name in sorted(inventory))
    return inventory, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _restore_projection_triggers(connection: sqlite3.Connection, inventory: dict[str, str], expected_hash: str) -> None:
    """Restore exactly the saved projection triggers and verify their hash."""
    if not inventory:
        return
    for name, sql in sorted(inventory.items()):
        current = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if current is None:
            connection.execute(sql)
        elif current[0] != sql:
            raise ValueError(f"merge changed protected trigger definition: {name}")
    _, actual_hash = _projection_trigger_inventory(connection)
    if actual_hash != expected_hash:
        raise ValueError("merge restored an unexpected source projection trigger inventory")


def _validate_staged_database(connection: sqlite3.Connection, destination: Path) -> None:
    """Validate the post-merge image before publishing its transaction.

    ``validate_snapshot_database(destination)`` cannot be called while this
    connection owns an uncommitted transaction on Windows. Serialize the
    connection image to a private sibling file, validate that complete image,
    then remove it. Any validation error therefore reaches the transaction
    rollback path before the destination is committed.
    """
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + destination.stem + ".merge-", suffix=".db", dir=destination.parent, delete=False) as handle:
            staged_path = Path(handle.name)
            handle.write(connection.serialize())
        validate_snapshot_database(staged_path)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def merge_database(incoming: Path, destination: Path) -> dict:
    """Union identical/disjoint rows and map colliding integer primary keys.

    UUID/natural-key collisions with different evidence are conflicts. Mapping
    touches registered identity envelopes, never arbitrary frozen provider text.
    All changes occur in a caller-owned staging database, in one transaction.
    """
    validate_snapshot_database(incoming)
    validate_snapshot_database(destination)
    with closing(readonly(incoming)) as source, closing(sqlite3.connect(destination)) as target:
        if _schema(source) != _schema(target):
            raise ValueError("merge requires identical registered database schemas")
        tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        shapes = {}
        mappings = {}
        counts = {}
        # Determine mappings before copying any dependent rows.
        for name in tables:
            info = list(source.execute(f"PRAGMA table_info({quote(name)})"))
            columns = [row[1] for row in info]
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            integer = len(keys) == 1 and next(row[2].upper() for row in info if row[1] == keys[0]) == "INTEGER"
            shapes[name] = (columns, keys, integer)
            mappings[name] = {}
            counts[name] = {"added": 0, "skipped": 0, "mapped": 0}
            if not integer:
                continue
            key = keys[0]
            maximum = max(source.execute(f"SELECT COALESCE(MAX({quote(key)}),0) FROM {quote(name)}").fetchone()[0],
                          target.execute(f"SELECT COALESCE(MAX({quote(key)}),0) FROM {quote(name)}").fetchone()[0])
            for row in source.execute(f"SELECT * FROM {quote(name)} ORDER BY {quote(key)}"):
                identity = row[columns.index(key)]
                # A previous disjoint merge may already have mapped this
                # neutral identity. Resolve its stable provider/part key before
                # allocating another integer, making repeated merges idempotent.
                natural = {"source_creators": ("platform", "external_id"),
                           "source_videos": ("platform", "external_id")}.get(name)
                if name == "video_parts":
                    natural = ("source_video_id", "page_index") if "source_video_id" in columns else ("bvid", "page_index")
                if natural and all(column in columns for column in natural):
                    values = [row[columns.index(column)] for column in natural]
                    if name == "video_parts" and natural[0] == "source_video_id":
                        values[0] = mappings["source_videos"].get(values[0], values[0])
                    if all(value is not None for value in values):
                        condition = " AND ".join(f"{quote(column)} IS ?" for column in natural)
                        existing_key = target.execute(f"SELECT {quote(key)} FROM {quote(name)} WHERE {condition}", values).fetchone()
                        if existing_key is not None:
                            if existing_key[0] != identity:
                                mappings[name][identity] = existing_key[0]
                                counts[name]["mapped"] += 1
                            continue
                existing = target.execute(f"SELECT * FROM {quote(name)} WHERE {quote(key)}=?", (identity,)).fetchone()
                if existing is not None and existing != row:
                    maximum += 1
                    mappings[name][identity] = maximum
                    counts[name]["mapped"] += 1
        def rewrite_envelope(value):
            if not isinstance(value, dict):
                return value
            result = dict(value)
            for key, (table, _) in _JSON_EDGES.items():
                if key in result:
                    result[key] = mappings[table].get(result[key], result[key])
            if isinstance(result.get("preserve_publication"), dict):
                result["preserve_publication"] = rewrite_envelope(result["preserve_publication"])
            return result
        foreign_keys = {}
        for name in tables:
            groups = defaultdict(list)
            for fk in source.execute(f"PRAGMA foreign_key_list({quote(name)})"):
                groups[fk[0]].append(fk)
            foreign_keys[name] = list(groups.values())

        def transformed(name, original):
            columns, keys, integer = shapes[name]
            row = list(original)
            if integer:
                index = columns.index(keys[0])
                row[index] = mappings[name].get(row[index], row[index])
            for group in foreign_keys[name]:
                if len(group) == 1:
                    fk = group[0]
                    index = columns.index(fk[3])
                    row[index] = mappings[fk[2]].get(row[index], row[index])
            for column in ("payload_json", "result_json", "evidence_json"):
                if column not in columns or row[columns.index(column)] is None:
                    continue
                if (name, column) not in {("workflow_jobs", "payload_json"), ("workflow_attempts", "result_json"), ("transcript_asr_evidence", "evidence_json")}:
                    continue
                index = columns.index(column)
                value = json.loads(row[index])
                rewritten = rewrite_envelope(value)
                if rewritten != value:
                    row[index] = json.dumps(rewritten, ensure_ascii=False, separators=(",", ":"))
            return row

        # A parent remap can change an otherwise identical child. Reach a fixed
        # point before inserting any rows, so its own integer key also maps.
        while True:
            added = False
            for name in tables:
                columns, keys, integer = shapes[name]
                if not integer:
                    continue
                key = keys[0]
                maximum = max([0, *mappings[name].values(), source.execute(f"SELECT COALESCE(MAX({quote(key)}),0) FROM {quote(name)}").fetchone()[0],
                               target.execute(f"SELECT COALESCE(MAX({quote(key)}),0) FROM {quote(name)}").fetchone()[0]])
                for original in source.execute(f"SELECT * FROM {quote(name)} ORDER BY {quote(key)}"):
                    identity = original[columns.index(key)]
                    if identity in mappings[name]:
                        continue
                    existing = target.execute(f"SELECT * FROM {quote(name)} WHERE {quote(key)}=?", (identity,)).fetchone()
                    if existing is not None and tuple(transformed(name, original)) != existing:
                        maximum += 1
                        mappings[name][identity] = maximum
                        counts[name]["mapped"] += 1
                        added = True
            if not added:
                break
        # Identity embedded in frozen prompts, prepared snapshots, segment refs,
        # publication documents and content digests is evidence, not an FK to
        # rewrite. A mapping of these histories requires a registered semantic
        # converter; refuse it before changing the staging database.
        protected = {"bilibili_users", "transcripts", "transcript_segments", "editorial_inputs",
                     "editorial_input_versions", "editorial_revisions", "editorial_model_calls",
                     "workflow_publications", "publication_editions", "publication_content_versions", "asr_models"}
        conflicts = sorted(name for name in protected if mappings.get(name))
        # Part IDs are also embedded in frozen editorial source snapshots and
        # ASR evidence. Metadata-only parts can map; parts with transcript
        # history need an explicit semantic converter rather than FK rewrites.
        if mappings.get("video_parts"):
            marks = ",".join("?" for _ in mappings["video_parts"])
            if source.execute(f"SELECT 1 FROM transcripts WHERE video_part_id IN ({marks}) LIMIT 1", tuple(mappings["video_parts"])).fetchone():
                conflicts.append("video_parts")
        if conflicts:
            raise ValueError("merge frozen-history identity conflict requires explicit replace or disjoint selection: " + ",".join(conflicts))
        # Validate both inventories before any mutation. A schema comparison
        # alone is not enough: the trigger set is the policy boundary that
        # decides whether legacy Bilibili inserts can create projections.
        source_projection, source_trigger_hash = _projection_trigger_inventory(source)
        target_projection, target_trigger_hash = _projection_trigger_inventory(target)
        if target_trigger_hash != source_trigger_hash or target_projection != source_projection:
            raise ValueError("merge requires identical source projection trigger inventories")
        foreign_keys_enabled = bool(target.execute("PRAGMA foreign_keys").fetchone()[0])
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("BEGIN IMMEDIATE")
        try:
            # Drop only projection writers. Identity and immutable-history
            # guards stay installed and protect source identity while rows are
            # copied. The exact definitions are restored before commit.
            for name in source_projection:
                target.execute(f"DROP TRIGGER {quote(name)}")
            for name in tables:
                columns, keys, integer = shapes[name]
                for original in source.execute(f"SELECT * FROM {quote(name)} ORDER BY rowid"):
                    row = transformed(name, original)
                    if keys:
                        condition = " AND ".join(f"{quote(key)} IS ?" for key in keys)
                        params = [row[columns.index(key)] for key in keys]
                        existing = target.execute(f"SELECT * FROM {quote(name)} WHERE {condition}", params).fetchone()
                    else:
                        condition = " AND ".join(f"{quote(column)} IS ?" for column in columns)
                        existing = target.execute(f"SELECT * FROM {quote(name)} WHERE {condition} LIMIT 1", row).fetchone()
                    if existing is not None:
                        if tuple(row) != existing:
                            raise ValueError("merge identity conflict; choose replace or a narrower selection: " + name)
                        counts[name]["skipped"] += 1
                        continue
                    try:
                        target.execute(f"INSERT INTO {quote(name)}({','.join(map(quote, columns))}) VALUES({','.join('?' for _ in columns)})", row)
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("merge unique identity conflict: " + name) from exc
                    counts[name]["added"] += 1
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("merge generated dangling foreign keys")
            _restore_projection_triggers(target, source_projection, source_trigger_hash)
            _validate_staged_database(target, Path(destination))
            target.commit()
        except BaseException:
            if target.in_transaction:
                target.rollback()
            raise
        finally:
            target.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")
    return {"tables": counts, "id_mappings": {name: [{"source": key, "target": value} for key, value in sorted(values.items())]
                                             for name, values in mappings.items() if values},
            "overwritten": 0, "conflicts": []}
