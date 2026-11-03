"""Explicit raw observation and frozen receipt closure for maintenance subsets."""
from __future__ import annotations

import json

from bili_asr.canonical_json import digest
from bili_asr.metadata_acceptance import acceptance_document, unpack_cache
from bili_asr.source_metadata import SourceMetadataSnapshot
from bili_asr.storage.observation_rows import _validated_row


def close_observations(connection, columns, keep, add, *, has_observations: bool) -> int:
    """Keep video-owned rows and dependencies without pulling creator siblings.

    A creator-page belongs to a selected video only through actual membership
    or an explicit receipt. Its original payload stays intact and is reported
    in the package boundary; its listed siblings never become authority rows.
    """
    changed = 0
    legacy = f"SELECT bvid FROM source.videos WHERE rowid IN (SELECT id FROM {keep('videos')})"
    if "source_videos" in columns:
        identities = (f"SELECT platform,external_id FROM source.source_videos "
                      f"WHERE rowid IN (SELECT id FROM {keep('source_videos')})")
    else:
        identities = f"SELECT 'bilibili',bvid FROM source.videos WHERE rowid IN (SELECT id FROM {keep('videos')})"
    if "source_metadata_observations" in columns:
        changed += add("source_metadata_observations", f"(platform,external_id) IN ({identities})")
    if "metadata_refresh_attempts" in columns:
        changed += add("metadata_refresh_attempts", f"bvid IN ({legacy})")
    if not has_observations:
        return changed

    changed += add("platform_observations", f"(platform,external_video_id) IN ({identities})")
    members = ("CASE WHEN json_type(payload_json,'$.list')='array' THEN json_extract(payload_json,'$.list') "
               "ELSE json_extract(payload_json,'$.list.vlist') END")
    changed += add("platform_observations", "platform='bilibili' AND operation='creator-page' AND EXISTS "
                   f"(SELECT 1 FROM json_each({members}) item WHERE "
                   f"CASE WHEN item.type='object' THEN json_extract(item.value,'$.bvid') END IN ({legacy}))")

    def receipt(binding):
        nonlocal changed
        identity, payload_hash, envelope_hash = binding
        row = connection.execute("SELECT * FROM source.platform_observations WHERE observation_id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("selected source receipt is missing: " + identity)
        observation = _validated_row(row)
        if observation.envelope['payloadSha256'] != payload_hash or digest(observation.envelope) != envelope_hash:
            raise ValueError("selected source receipt digest differs: " + identity)
        changed += add("platform_observations", "observation_id=?", (identity,))

    def document_binding(value):
        receipt((value['observationId'], value['payloadSha256'], value['envelopeSha256']))

    if "source_metadata_observations" in columns:
        for platform, external_id, field, raw in connection.execute(
            f"SELECT platform,external_id,field,value_json FROM source.source_metadata_observations "
            f"WHERE rowid IN (SELECT id FROM {keep('source_metadata_observations')})"
        ):
            if raw is None:
                continue
            if platform == 'bilibili':
                _, cache = unpack_cache(raw)
                if cache is not None:
                    document_binding(cache['acceptance'])
                    document_binding(cache['receipt']['source'])
                    # The adopted parts value is a video-wide fact. Keep its
                    # complete authority collection so cache validation remains
                    # exact even when the request initially names one part.
                    if field == 'parts':
                        changed += add('video_parts', 'bvid=?', (external_id,))
                    elif field == 'tags':
                        changed += add('video_tags', 'bvid=?', (external_id,))
                        changed += add('video_tag_observations', 'bvid=?', (external_id,))
                    elif field in {'pic', 'desc', 'tid'}:
                        changed += add('video_details', 'bvid=?', (external_id,))
            elif platform == 'youtube':
                cache = json.loads(raw)
                if cache.get('observation') is not None:
                    document_binding(cache['observation'])

    for table, column, path in (
        ('editorial_inputs', 'prepared_json', '$.snapshot.metadata.source_metadata'),
        ('publication_editions', 'content_json', '$.source.metadata'),
    ):
        for (raw,) in connection.execute(
            f'SELECT json_extract("{column}",?) FROM source."{table}" '
            f'WHERE rowid IN (SELECT id FROM {keep(table)}) '
            f'AND json_extract("{column}",?)=\'source-metadata-v2\'', (path, path + '.version')
        ):
            frozen = SourceMetadataSnapshot.from_dict(json.loads(raw))
            for binding in frozen.observation_bindings:
                receipt(binding)

    for row in connection.execute(
        f"SELECT * FROM source.platform_observations WHERE operation='accepted-metadata' "
        f"AND rowid IN (SELECT id FROM {keep('platform_observations')})"
    ):
        adoption = acceptance_document(_validated_row(row))
        for field in adoption['fields']:
            document_binding(field['source'])
    return changed
