"""Invalid character timings must be refused before an archive is published."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bili_asr.archive import characters_for, write_archive
from bili_asr.page_identity import page_identity


_SEGMENTS = [{"start": 0.0, "end": 1.0, "text": "x"}]


def _entry() -> dict:
    identity = page_identity("BV1chartiming", 0, 7)
    return {
        "bvid": identity.bvid,
        "work_id": identity.work_id,
        "page_index": 0,
        "cid": 7,
        "title": "Character timing",
        "pubdate_str": "2026-01-02",
        "duration_s": 1,
    }


@pytest.mark.parametrize("field", ["starts", "ends"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(float("nan"), "must be finite", id="nan"),
        pytest.param(float("inf"), "must be finite", id="infinity"),
        pytest.param(float("-inf"), "must be finite", id="negative-infinity"),
        pytest.param(True, "must be numbers", id="true"),
        pytest.param(False, "must be numbers", id="false"),
        pytest.param(-0.1, "must not be negative", id="negative"),
    ],
)
def test_invalid_character_time_does_not_publish(tmp_root, field, value, message) -> None:
    root = Path(tmp_root)
    record = {"text": "x", "starts": [0.0], "ends": [1.0]}
    record[field] = [value]

    with pytest.raises(ValueError, match=message):
        write_archive(root, _entry(), _SEGMENTS, source="asr", characters=record)

    assert not list(root.rglob("bundle.*"))


def test_invalid_character_update_preserves_the_published_bundle(tmp_root) -> None:
    root = Path(tmp_root)
    paths = write_archive(
        root, _entry(), _SEGMENTS, source="asr",
        characters={"text": "x", "starts": [0.0], "ends": [1.0]},
    )
    published = {path: (root / path).read_bytes() for path in paths.values()}

    with pytest.raises(ValueError, match="must be finite"):
        write_archive(
            root, _entry(), _SEGMENTS, source="asr",
            characters={"text": "x", "starts": [float("nan")], "ends": [1.0]},
        )

    assert {path: (root / path).read_bytes() for path in paths.values()} == published
    # A strict JSON consumer can read the complete record after the rejected update.
    raw = json.loads(
        (root / paths["raw_path"]).read_text(encoding="utf-8"),
        parse_constant=lambda token: pytest.fail(f"non-standard JSON constant: {token}"),
    )
    assert raw["characters"] == {"text": "x", "starts": [0.0], "ends": [1.0]}


def test_zero_length_and_tied_character_times_remain_valid() -> None:
    segments = [{"start": 0.0, "end": 1.0, "text": "xy"}]
    record = {"text": "xy", "starts": [0, 0.0], "ends": [0.0, 0]}
    result = characters_for(segments, record)

    assert result == record
    assert result["starts"] is not record["starts"]
    assert result["ends"] is not record["ends"]


def test_large_integer_times_do_not_require_a_float_conversion() -> None:
    # Python integers remain exact JSON numbers; the gate does not round the aligner record.
    instant = 10 ** 400
    record = {"text": "x", "starts": [instant], "ends": [instant]}
    assert characters_for(_SEGMENTS, record) == record
