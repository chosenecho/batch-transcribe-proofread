from __future__ import annotations
import itertools
import os
import pytest
from bili_asr.cli.main import main
from bili_asr.config import DEFAULT_PAGE_LIMIT
from bili_asr.storage import open_database
from tests.fixtures.fake_bilibili_gateway import (
    MID,
    RAW_JSON_BODY_MARKER,
    SESSDATA_BOUNDARY_VALUE,
    SIGNED_URL_MARKER,
    UPSTREAM_ERROR_TEXT,
    FakeResponseCodeException,
    assert_leaks_no_markers,
    assert_only_documented_metadata_calls,
    bilibili_api_seam,
    make_part_item,
    make_videos_response,
    make_vlist_item,
    persisted_row_text,
    script_parts_by_bvid,
)


LEGACY_SIDECAR_PATHS = (
    os.path.join("manifest", "manifest.jsonl"),
    "meta-cursor.json",
    "run-ledger.jsonl",
)
