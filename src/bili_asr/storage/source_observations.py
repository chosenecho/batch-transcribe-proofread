"""Append-only original observations, validated optional schema and offline reads."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from bili_asr.canonical_json import digest
from bili_asr.source_observations import PlatformObservation, project_superset
from bili_asr.storage.observation_rows import (
    _validated_row,
    append_observations,
    install_observations_in_staged_copy,
    observation_objects,
    require_observations,
)

__all__ = [
    "ObservationRepository", "install_observations_in_staged_copy",
    "observation_objects", "require_observations", "verify_frozen_source_bindings",
]

def verify_frozen_source_bindings(connection: sqlite3.Connection) -> None:
    """Verify historical v2 references, even after current facts have refreshed."""
    from bili_asr.source_metadata import SourceMetadataSnapshot

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = (("editorial_inputs", "prepared_json", "$.snapshot.metadata.source_metadata"),
               ("publication_editions", "content_json", "$.source.metadata"))
    repository = ObservationRepository(connection)
    for table, column, pointer in sources:
        if table not in tables:
            continue
        query = (f"SELECT json_extract({column},?) FROM {table} WHERE CASE WHEN json_valid({column}) "
                 f"THEN json_extract({column},?) END='source-metadata-v2'")
        for row in connection.execute(query, (pointer, pointer + ".version")):
            source = SourceMetadataSnapshot.from_dict(json.loads(row[0]))
            for identity, payload_digest, envelope_digest in source.observation_bindings:
                observation = repository.get(identity)
                envelope = observation.envelope
                scope = envelope["scope"]
                if (envelope["platform"] != source.ref.platform or envelope["state"] != "success"
                        or envelope["payloadSha256"] != payload_digest or digest(envelope) != envelope_digest
                        or ("partIndex" in scope and scope["partIndex"] != source.ref.part_index)):
                    raise ValueError("frozen source observation digest or scope differs")
                if envelope["operation"] == "accepted-metadata":
                    from bili_asr.metadata_acceptance import (
                        acceptance_document,
                        validate_frozen_acceptance,
                    )
                    from bili_asr.storage.metadata_acceptance import (
                        verify_acceptance_graph,
                    )
                    accepted = acceptance_document(observation)
                    validate_frozen_acceptance(accepted, source)
                    verify_acceptance_graph(connection, observation, owner_mid=int(source.creator_id))
                    continue
                if scope.get("externalVideoId") == source.ref.external_video_id:
                    continue
                payload = json.loads(observation.payload_json or "null")
                listing = payload.get("list") if isinstance(payload, dict) else None
                page = listing.get("vlist") if isinstance(listing, dict) else listing
                if (source.ref.platform != "bilibili" or envelope["operation"] != "creator-page"
                        or not isinstance(page, list) or not any(isinstance(item, dict) and
                            item.get("bvid") == source.ref.external_video_id for item in page)):
                    raise ValueError("frozen source observation belongs to a different video")


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(self, observations: Iterable[PlatformObservation]) -> None:
        append_observations(self.connection, observations)

    def get(self, observation_id: str) -> PlatformObservation:
        if (not isinstance(observation_id, str) or len(observation_id) != 32
                or any(character not in "0123456789abcdef" for character in observation_id)):
            raise ValueError("invalid platform observation ID")
        if not require_observations(self.connection):
            raise ValueError("raw platform observation evidence is unavailable")
        row = self.connection.execute("SELECT * FROM platform_observations WHERE observation_id=?",
                                      (observation_id,)).fetchone()
        if row is None:
            raise ValueError("raw platform observation ID is unavailable")
        return _validated_row(row)

    def read_video(self, platform: str, external_video_id: str, *, limit: int = 1000,
                   observed_after: int | None = None, observed_before: int | None = None,
                   payload_byte_limit: int = 64 * 1024 * 1024,
                   include_acceptances: bool = True) -> tuple[PlatformObservation, ...]:
        if type(include_acceptances) is not bool:
            raise ValueError("acceptance selection must be explicit boolean")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("observation read limit must be 1 to 10000")
        for stamp in (observed_after, observed_before):
            if stamp is not None and (type(stamp) is not int or not 0 <= stamp <= 2**63 - 1):
                raise ValueError("observation range must use nonnegative Unix timestamps")
        if observed_after is not None and observed_before is not None and observed_after > observed_before:
            raise ValueError("observation range begins after it ends")
        if type(payload_byte_limit) is not int or not 1 <= payload_byte_limit <= 256 * 1024 * 1024:
            raise ValueError("observation byte budget must be 1 to 268435456")
        if not require_observations(self.connection):
            return ()
        # Creator-page observations carry all listed videos without duplicating
        # a potentially large page for each video. Bind membership by raw JSON.
        rows = self.connection.execute(
            "SELECT * FROM platform_observations WHERE platform=? AND (external_video_id=? OR "
            "(operation='creator-page' AND EXISTS(SELECT 1 FROM json_each(payload_json,'$.list.vlist') v "
            "WHERE CASE WHEN v.type='object' THEN json_extract(v.value,'$.bvid') END=?))) "
            "AND (? IS NULL OR observed_at>=?) AND (? IS NULL OR observed_at<=?) "
            "AND (? OR operation<>'accepted-metadata') "
            "ORDER BY observed_at,rowid LIMIT ?",
            (platform, external_video_id, external_video_id, observed_after, observed_after,
             observed_before, observed_before, int(include_acceptances), limit + 1))
        observations: list[PlatformObservation] = []
        retained_bytes = 0
        for row in rows:
            observation = _validated_row(row)
            retained_bytes += len(observation.envelope_json.encode("utf-8"))
            retained_bytes += len((observation.payload_json or "").encode("utf-8"))
            if len(observations) == limit or retained_bytes > payload_byte_limit:
                raise ValueError("observation read budget exceeded; select a bounded observation range")
            observations.append(observation)
        return tuple(observations)

    def superset(self, platform: str, external_video_id: str, *, mapping_version: str = "source-superset/v1",
                 limit: int = 1000, observed_after: int | None = None,
                 observed_before: int | None = None) -> dict[str, Any]:
        report = project_superset(self.read_video(platform, external_video_id, limit=limit,
            observed_after=observed_after, observed_before=observed_before, include_acceptances=False), platform=platform,
            external_video_id=external_video_id, mapping_version=mapping_version)
        report["coverage"]["selection"] = {"observedAfterInclusive": observed_after,
            "observedBeforeInclusive": observed_before, "limit": limit}
        return report

    def verify(self) -> None:
        if not require_observations(self.connection):
            return
        for row in self.connection.execute("SELECT * FROM platform_observations"):
            observation = _validated_row(row)
            if observation.envelope["operation"] == "accepted-metadata":
                from bili_asr.storage.metadata_acceptance import verify_acceptance_graph
                verify_acceptance_graph(self.connection, observation)

    def read_videos(self, platform: str, external_ids: tuple[str, ...]) -> dict[str, tuple[PlatformObservation, ...]]:
        """Bounded set read for frozen Bilibili metadata, without one query per part."""
        if len(external_ids) > 256 or len(set(external_ids)) != len(external_ids):
            raise ValueError("observation batch requires at most 256 distinct video IDs")
        result: dict[str, list[PlatformObservation]] = {identity: [] for identity in external_ids}
        if not external_ids or not require_observations(self.connection):
            return {identity: () for identity in external_ids}
        marks = ",".join("?" for _ in external_ids)
        rows = self.connection.execute(
            f"SELECT * FROM platform_observations WHERE platform=? AND (external_video_id IN ({marks}) OR "
            f"(operation='creator-page' AND EXISTS(SELECT 1 FROM json_each(payload_json,'$.list.vlist') v "
            f"WHERE CASE WHEN v.type='object' THEN json_extract(v.value,'$.bvid') END IN ({marks})))) ORDER BY observed_at,rowid LIMIT 10001",
            (platform, *external_ids, *external_ids))
        byte_count = 0
        for count, row in enumerate(rows):
            observation = _validated_row(row)
            byte_count += len(observation.envelope_json.encode("utf-8")) + len((observation.payload_json or "").encode("utf-8"))
            if count == 10000 or byte_count > 64 * 1024 * 1024:
                raise ValueError("observation batch budget exceeded")
            envelope = observation.envelope
            identity = envelope["scope"].get("externalVideoId")
            if identity in result:
                result[identity].append(observation)
            elif envelope["operation"] == "creator-page":
                import json
                payload = json.loads(observation.payload_json or "null")
                listing = payload.get("list") if isinstance(payload, dict) else None
                page = listing.get("vlist") if isinstance(listing, dict) else None
                if isinstance(page, list):
                    membership = {item.get("bvid") for item in page if isinstance(item, dict) and isinstance(item.get("bvid"), str)}
                    for matching in result.keys() & membership:
                        result[matching].append(observation)
        return {identity: tuple(values) for identity, values in result.items()}
