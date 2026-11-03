"""Current database source facts survive the real JSON/CSV export path."""
from __future__ import annotations

import csv
import io
import json

import pytest

from bili_asr.export import (
    SOURCE_METADATA_CSV_COLUMNS, STANDARD_CSV_COLUMNS, export_records,
    format_csv_export, sanitize_export_entry,
)
from bili_asr.services.archive_migration import initialize_archive
from bili_asr.services.workflow_projection import workflow_records
from bili_asr.storage import MetadataRepository, open_database
from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
from bili_asr.platform_identity import ContentRef
from tests.test_workflow_control_plane import _seed_part


DESCRIPTION = 'https://example.org/design-guide\n中文：“原简介”\\目录: P1\nhttps://example.org/authors/overview'


@pytest.mark.parametrize("universal", [False, True])
def test_real_multi_part_export_keeps_description_and_collection_semantics(tmp_path, universal):
    if universal:
        initialize_archive(tmp_path)
    connection = open_database(tmp_path)
    _seed_part(connection)
    with connection:
        connection.execute('INSERT INTO video_details VALUES (?,NULL,?,NULL,?)', ('BVtest', DESCRIPTION, 777))
        connection.execute("UPDATE videos SET updated_at=888 WHERE bvid='BVtest'")
        connection.execute("""INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,
                           duration_ms,processing_status,created_at,updated_at)
                           VALUES (2,'BVtest',1,2,'第二 P',1000,'metadata_collected',1,1)""")
        connection.executemany("INSERT INTO video_tags VALUES ('BVtest',?,?,'topic')", [(3, '重复'), (1, '中文'), (2, '重复')])
        connection.execute("INSERT INTO video_tag_observations VALUES ('BVtest','unavailable',999,'transport_error',NULL)")
    connection.close()

    rows = json.loads(export_records(tmp_path, "json"))
    assert len(rows) == 2
    for row in rows:
        assert row['video_description'] == DESCRIPTION
        assert row['video_details_observed_at'] == 777
        assert row['uploader_mid'] == 1 and row['uploader_name'] == 'test'
        assert row['video_tags'] == ['中文', '重复']
        assert row['video_tags_state'] == 'unavailable'
        if universal:
            assert row['sourceMetadata']['description'] == DESCRIPTION
            assert row['sourceMetadata']['metadataObservedAt'] == 888
    assert [row['title'] for row in rows] == ['p0', '第二 P']
    csv_rows = list(csv.DictReader(io.StringIO(export_records(tmp_path, "csv", with_text=True, private=True))))
    assert csv_rows[0]['video_description'] == DESCRIPTION
    assert json.loads(csv_rows[0]['video_tags']) == ['中文', '重复']
    assert csv_rows[0]['transcript_text'] == ''


@pytest.mark.parametrize("state", [None, 'success_empty', 'success_nonempty', 'unavailable'])
def test_missing_detail_and_tag_observation_are_independent(tmp_path, state):
    connection = open_database(tmp_path)
    _seed_part(connection)
    if state:
        with connection:
            connection.execute("INSERT INTO video_tag_observations VALUES ('BVtest',?,1,NULL,NULL)", (state,))
    connection.close()
    row = json.loads(export_records(tmp_path, 'json'))[0]
    assert row['video_description'] is None
    assert row['video_details_observed_at'] is None
    assert row['video_tags'] == [] and row['video_tags_state'] == state
    csv_row = next(csv.DictReader(io.StringIO(export_records(tmp_path, 'csv'))))
    assert csv_row['video_description'] == ''


def test_old_archive_without_observation_table_keeps_unknown_state(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    with connection:
        connection.execute('DROP TABLE video_tag_observations')
        connection.execute("INSERT INTO video_tags VALUES ('BVtest',1,'旧标签','topic')")
    connection.close()
    row = next(iter(workflow_records(tmp_path).values()))
    assert row['video_tags'] == ['旧标签']
    assert row['video_tags_state'] is None


def test_description_policy_preserves_links_without_relaxing_other_urls():
    ordinary = ('https://example.org/authors/overview\nhttps://example.org/design-guide'
                '\nhttps://example.org/reference?design=course&authentication=public')
    secret = ('https://example.org/file?token=private\nhttps://example.org/file?SIGN=private\n'
              'https://user:private@example.org/\nhttps://a.bilivideo.com/media\n'
              'https://s3.example.org/file?X-Amz-Credential=private\nSESSDATA=private\npassword=private')
    row = sanitize_export_entry({
        'video_description': ordinary + '\n' + secret,
        'sourceMetadata': {'description': ordinary + '\n' + secret},
        'title': ordinary, 'stream_url': ordinary, 'token': 'private',
    })
    assert row['video_description'].startswith(ordinary + '\n')
    assert 'private' not in row['video_description']
    assert row['sourceMetadata']['description'] == row['video_description']
    assert row['title'] == '[redacted]' and 'stream_url' not in row and 'token' not in row


def test_csv_source_columns_append_after_previous_text_and_custom_columns():
    old = {key: key for key in STANDARD_CSV_COLUMNS}
    old.update(transcript_text='body', z_custom='value')
    new = {**old, **dict.fromkeys(SOURCE_METADATA_CSV_COLUMNS)}
    header = next(csv.reader(io.StringIO(format_csv_export([new], with_text=True))))
    assert header == [*STANDARD_CSV_COLUMNS, 'transcript_text', 'z_custom', *SOURCE_METADATA_CSV_COLUMNS]
    empty = next(csv.reader(io.StringIO(format_csv_export([]))))
    assert empty == [*STANDARD_CSV_COLUMNS, *SOURCE_METADATA_CSV_COLUMNS]


def test_metadata_batch_queries_do_not_scale_with_parts(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    with connection:
        connection.executemany(
            """INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,
               duration_ms,processing_status,created_at,updated_at)
               VALUES (?,'BVtest',?,?,'part',1000,'metadata_collected',1,1)""",
            [(index + 1, index, index + 1) for index in range(1, 150)],
        )
    statements = []
    connection.set_trace_callback(statements.append)
    values = MetadataRepository(connection).read_source_metadata_export_many(range(1, 151))
    connection.set_trace_callback(None)
    assert len(values) == 150
    assert sum('FROM video_tags WHERE' in sql for sql in statements) == 1
    assert sum('FROM video_tag_observations WHERE' in sql for sql in statements) == 1
    connection.close()


def test_legacy_flat_export_does_not_apply_frozen_snapshot_description_limit(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    description = '长' * 65537
    with connection:
        connection.execute('INSERT INTO video_details VALUES (?,NULL,?,NULL,1)', ('BVtest', description))
    connection.close()
    assert json.loads(export_records(tmp_path, 'json'))[0]['video_description'] == description


def test_non_bilibili_projection_does_not_fabricate_bilibili_fields(tmp_path):
    initialize_archive(tmp_path)
    connection = open_database(tmp_path)
    with connection:
        SourceRepository(connection).upsert_video(SourceVideoMetadata(
            ContentRef('youtube', 'dQw4w9WgXcQ', 0), 'Source', 1000, 'UCcreator', 'Creator', observed_at=5,
        ))
    connection.close()
    row = json.loads(export_records(tmp_path, 'json'))[0]
    assert row['video_description'] is None and row['uploader_mid'] is None
    assert row['uploader_name'] == 'Creator'
    assert row['video_tags_state'] is None and row['video_details_observed_at'] is None
