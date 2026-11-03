import pytest

from bili_asr.page_identity import (
    PageIdentity,
    artifact_stem,
    format_work_id,
    page_identity,
    parse_work_id,
)


def test_format_and_parse_round_trip():
    work_id = format_work_id("BV1aa", 0)
    assert work_id == "BV1aa:p0"
    assert parse_work_id(work_id) == ("BV1aa", 0)


def test_artifact_stem_has_no_colon():
    identity = page_identity("BV1aa", 2, cid=99, page_label="part")
    stem = artifact_stem(identity)
    assert stem == "BV1aa.p2"
    assert ":" not in stem
    assert ":" in identity.work_id


def test_parse_work_id_rejects_garbage():
    with pytest.raises(ValueError):
        parse_work_id("BV1aa")
    with pytest.raises(ValueError):
        parse_work_id("BV1aa:p")


def test_page_identity_is_frozen():
    identity = PageIdentity(
        work_id="BV1aa:p0",
        bvid="BV1aa",
        page_index=0,
        cid=1,
        page_label="",
    )
    with pytest.raises(Exception):
        identity.cid = 2  # type: ignore[misc]
