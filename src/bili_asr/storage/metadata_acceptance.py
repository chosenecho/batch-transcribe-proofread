"""Transactional accepted metadata and bounded offline receipt verification."""
from __future__ import annotations

from collections.abc import Iterable

from bili_asr.canonical_json import canonical
from bili_asr.metadata_acceptance import (
    CONTRACT,
    ENDPOINT,
    FIELD_OPERATIONS,
    OPERATION,
    SourceFieldReceipt,
    acceptance_document,
    binding,
    binding_tuple,
    unpack_cache,
)
from bili_asr.source_observations import PlatformObservation
from bili_asr.sources.bilibili_metadata_codec import (
    receipt_values,
    verify_source_receipt,
)
from bili_asr.storage.observation_rows import (
    _validated_row,
    append_observations,
    require_observations,
)

MAX_READ_BYTES = 64 * 1024 * 1024
MAX_COLLECTION_ROWS = 100000


def current_values_many(connection, bvids: tuple[str, ...]) -> dict[str, dict]:
    """Read current values with four set queries, irrespective of history depth."""
    if not bvids:
        return {}
    if len(bvids) > 256:
        raise ValueError("metadata acceptance batch is limited to 256 videos")
    marks = ",".join("?" for _ in bvids)
    result = {}
    for row in connection.execute(f'SELECT v.bvid,v.mid,v.title,v.pubdate,v.aid,d.pic,d."desc",d.tid '
            f'FROM videos v LEFT JOIN video_details d USING(bvid) WHERE v.bvid IN ({marks})', bvids):
        result[row[0]] = dict(zip(("mid", "title", "pubdate", "aid", "pic", "desc", "tid"), row[1:], strict=True))
        result[row[0]].update(parts=[], tags=[])
    byte_count = row_count = 0
    for table, columns, field, names, ordering in (
            ("video_parts", "page_index,cid,title,duration_ms", "parts", ("pageIndex", "cid", "title", "durationMs"), "page_index"),
            ("video_tags", "tag_id,tag_name,tag_type", "tags", ("tagId", "tagName", "tagType"), "tag_id")):
        for row in connection.execute(f"SELECT bvid,{columns} FROM {table} WHERE bvid IN ({marks}) ORDER BY bvid,{ordering}", bvids):
            value = dict(zip(names, row[1:], strict=True))
            row_count += 1
            byte_count += len(canonical(value).encode("utf-8"))
            if row_count > MAX_COLLECTION_ROWS or byte_count > MAX_READ_BYTES:
                raise ValueError("current accepted metadata exceeds collection read budget")
            if row[0] not in result:
                raise ValueError("current metadata part or tag has no owning video")
            result[row[0]][field].append(value)
    return result


def _read_receipts(connection, expected: dict[str, tuple[str, str, str]]) -> dict[str, PlatformObservation]:
    """Only selected IDs are read; SQLite variable and memory budgets are bounded."""
    if not expected:
        return {}
    require_observations(connection, required=True)
    identities = tuple(expected)
    found = {}
    total = 0
    for start in range(0, len(identities), 256):
        batch = identities[start:start + 256]
        marks = ",".join("?" for _ in batch)
        for row in connection.execute(f"SELECT * FROM platform_observations WHERE observation_id IN ({marks})", batch):
            row = dict(row) if hasattr(row, "keys") else dict(zip(
                ("observation_id", "platform", "operation", "external_video_id", "creator_id", "observed_at",
                 "envelope_json", "envelope_sha256", "payload_json"), row, strict=True))
            # Apply the budget before parsing/normalizing large private documents.
            total += len((row["payload_json"] or "").encode("utf-8")) + len(row["envelope_json"].encode("utf-8"))
            if total > MAX_READ_BYTES:
                raise ValueError("selected metadata receipts exceed read budget")
            observation = _validated_row(row)
            if binding_tuple(binding(observation)) != expected[observation.observation_id]:
                raise ValueError("accepted metadata receipt digest differs")
            found[observation.observation_id] = observation
    if found.keys() != expected.keys():
        raise ValueError("accepted metadata source receipt is missing")
    return found


def _expect(expected: dict, item: dict) -> None:
    value = binding_tuple(item)
    if value[0] in expected and expected[value[0]] != value:
        raise ValueError("conflicting accepted metadata receipt bindings")
    expected[value[0]] = value


def record_acceptance(connection, bvid: str, receipts: Iterable[SourceFieldReceipt], observed_at: int) -> None:
    """Append selection and update its cache inside the normalized caller transaction."""
    receipts = tuple(receipts)
    if not receipts:
        return
    if not connection.in_transaction:
        raise ValueError("accepted metadata must share a normalized write transaction")
    if (len(receipts) > 8 or any(not isinstance(receipt, SourceFieldReceipt) for receipt in receipts)
            or len({receipt.field for receipt in receipts}) != len(receipts)):
        raise ValueError("accepted metadata fields must be unique and bounded")
    expected = {}
    for receipt in receipts:
        _expect(expected, receipt.document["source"])
    originals = _read_receipts(connection, expected)
    current = current_values_many(connection, (bvid,)).get(bvid)
    if current is None:
        raise ValueError("accepted metadata video is not archived")
    selected = []
    normalized = {}
    for receipt in receipts:
        document = receipt.document
        key = (document["source"]["observationId"], document["normalizer"], canonical(document["context"]))
        if key not in normalized:
            normalized[key] = receipt_values(originals[key[0]], document["normalizer"], document["context"])
        verify_source_receipt(receipt, originals[key[0]], bvid=bvid, owner_mid=current["mid"], normalized=normalized[key])
        # Stable aid and publication policies may deliberately retain a prior
        # value. Only the DTO value that actually reached storage is accepted.
        if canonical(current[receipt.field]) == canonical(document["value"]):
            selected.append(document)
    if not selected:
        return
    adoptions = []
    for document in selected:
        # A partial future refresh may retain other fields from this read.
        # Independent field nodes keep that historical selection exact.
        acceptance = PlatformObservation.capture(platform="bilibili", operation=OPERATION, endpoint=ENDPOINT,
            scope={"externalVideoId": bvid}, extractor="bili-asr", extractor_version=CONTRACT,
            observed_at=observed_at, payload={"version": CONTRACT, "externalVideoId": bvid, "fields": [document]})
        adoptions.append((document, acceptance))
    append_observations(connection, (acceptance for _, acceptance in adoptions))
    for document, acceptance in adoptions:
        field = document["field"]
        raw = originals[document["source"]["observationId"]]
        stamp = raw.envelope["observedAt"]
        cache = {"version": CONTRACT, "value": document["value"], "receipt": document, "acceptance": binding(acceptance)}
        connection.execute("""INSERT INTO source_metadata_observations VALUES ('bilibili',?,?,?,?,?,?,NULL,?)
            ON CONFLICT(platform,external_id,operation,field) DO UPDATE SET state=excluded.state,
              observed_at=excluded.observed_at,last_success_at=excluded.last_success_at,
              error_code=NULL,value_json=excluded.value_json""",
            (bvid, FIELD_OPERATIONS[field], field, "empty" if document["value"] in (None, []) else "present",
             observed_at, stamp, canonical(cache)))


def read_bindings_many(connection, bvids: tuple[str, ...]) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Verify current adopted DTOs by their selected IDs, never latest raw rows."""
    if not bvids:
        return {}
    if len(bvids) > 256:
        raise ValueError("metadata acceptance batch is limited to 256 videos")
    marks = ",".join("?" for _ in bvids)
    caches = []
    accepted_ids, source_ids = {}, {}
    total = 0
    for row in connection.execute(f"SELECT * FROM source_metadata_observations WHERE platform='bilibili' "
            f"AND external_id IN ({marks}) ORDER BY external_id,operation,field", bvids):
        row = dict(row) if hasattr(row, "keys") else dict(zip(
            ("platform", "external_id", "operation", "field", "state", "observed_at", "last_success_at",
             "error_code", "value_json"), row, strict=True))
        total += len((row["value_json"] or "").encode("utf-8"))
        if total > MAX_READ_BYTES:
            raise ValueError("accepted metadata cache exceeds read budget")
        _, cache = unpack_cache(row["value_json"])
        if cache is None:
            continue
        document = cache["receipt"]
        if (document["field"] != row["field"] or FIELD_OPERATIONS[row["field"]] != row["operation"]
                or document["context"].get("bvid") != row["external_id"] or row["last_success_at"] is None
                or (row["state"] in {"present", "empty"} and row["state"] != (
                    "empty" if document["value"] in (None, []) else "present"))):
            raise ValueError("accepted metadata cache scope or field differs")
        _expect(accepted_ids, cache["acceptance"])
        _expect(source_ids, document["source"])
        caches.append((dict(row), cache))
    accepted = _read_receipts(connection, accepted_ids)
    originals = _read_receipts(connection, source_ids)
    current = current_values_many(connection, bvids) if caches else {}
    accepted_docs = {identity: acceptance_document(observation) for identity, observation in accepted.items()}
    normalized = {}
    results = {bvid: set() for bvid in bvids}
    for row, cache in caches:
        bvid, document = row["external_id"], cache["receipt"]
        adoption = accepted_docs[cache["acceptance"]["observationId"]]
        if (bvid not in current or adoption["externalVideoId"] != bvid or document not in adoption["fields"]
                or canonical(current[bvid][row["field"]]) != canonical(cache["value"])):
            raise ValueError("accepted metadata cache differs from immutable adoption or stored facts")
        key = (document["source"]["observationId"], document["normalizer"], canonical(document["context"]))
        source = originals[key[0]]
        if row["last_success_at"] != source.envelope["observedAt"]:
            raise ValueError("accepted metadata acquisition timestamp differs")
        if key not in normalized:
            normalized[key] = receipt_values(source, document["normalizer"], document["context"])
        verify_source_receipt(SourceFieldReceipt(canonical(document)), source, bvid=bvid,
                              owner_mid=current[bvid]["mid"], normalized=normalized[key])
        results[bvid].add(binding_tuple(cache["acceptance"]))
        results[bvid].add(binding_tuple(document["source"]))
    return {bvid: tuple(sorted(values)) for bvid, values in results.items()}


def invalidate_fields(connection, bvid: str, fields: tuple[str, ...]) -> None:
    """A legacy direct mutation makes changed facts' raw selection unknown."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_metadata_observations' AND type='table'").fetchone() is None:
        return
    for field in fields:
        if field not in FIELD_OPERATIONS:
            raise ValueError("unknown metadata field to invalidate")
        # Called only when the normalized write really changes this field.
        connection.execute("UPDATE source_metadata_observations SET value_json=NULL,last_success_at=NULL "
            "WHERE platform='bilibili' AND external_id=? AND field=? AND json_type(value_json)='object'",
            (bvid, field))


def verify_acceptance_graph(connection, observation: PlatformObservation, *, owner_mid: int | None = None) -> dict:
    """Validate immutable historical selection, independently of current cache."""
    document = acceptance_document(observation)
    expected = {}
    for item in document["fields"]:
        _expect(expected, item["source"])
    sources = _read_receipts(connection, expected)
    normalized = {}
    for item in document["fields"]:
        key = (item["source"]["observationId"], item["normalizer"], canonical(item["context"]))
        if key not in normalized:
            normalized[key] = receipt_values(sources[key[0]], item["normalizer"], item["context"])
        verify_source_receipt(SourceFieldReceipt(canonical(item)), sources[key[0]], bvid=document["externalVideoId"],
                              owner_mid=owner_mid, normalized=normalized[key])
    return document
