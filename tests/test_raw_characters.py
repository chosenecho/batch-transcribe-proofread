"""The character-level record ``bundle.raw.json`` carries on the ASR path.

``_thread_text`` is the last step that still knows which character of the transcript each instant
belongs to (``asr.py``); ``_aligned_cues`` then projects that stream onto readable cue text and, in
doing so, drops the whitespace that sat on a cue boundary.  This suite pins the record built in
between — the thing that makes a future cue-rule change a CPU re-slice instead of another 37-minute
GPU run.

Two of these tests are negative controls, and they are the ones that matter: a subtitle-derived raw
must **not** carry a fabricated ``characters`` block, and an **old** artifact (written before this
field existed) must still be readable by every reader that touches it.
"""

from __future__ import annotations

import bili_asr.asr.alignment as _module_asr_alignment
import bili_asr.asr.audio as _module_asr_audio
import bili_asr.asr.config as _module_asr_config
import bili_asr.asr.constants as _module_asr_constants
import bili_asr.asr.coverage as _module_asr_coverage
import bili_asr.asr.runner as _module_asr_runner


import json

import pytest

from bili_asr import asr
from bili_asr.archive import write_archive
from bili_asr.page_identity import page_identity

#: The aligner times the characters **without** punctuation: a mark has no audio to align to, so it
#: never comes back in the unit list.  Same set the boundary and the fakes use.
_MARKS = "。！？!?，、；：,;: "


def _units(text: str, step: float = 0.4) -> list[dict]:
    """One timed unit per timeable character of ``text``, as the aligner really returns."""

    return [
        {"text": char, "start_time": index * step, "end_time": index * step + step - 0.05}
        for index, char in enumerate(text)
        if char not in _MARKS
    ]


def _record(text: str) -> tuple[list[dict], dict]:
    """Run the two pure steps the way ``ASRRunner.transcribe`` does, and return cues + record."""

    pieces = _module_asr_alignment._thread_text(text, [dict(unit) for unit in _units(text)])
    cues = _module_asr_alignment._aligned_cues([dict(piece) for piece in pieces])
    return cues, _module_asr_alignment._characters_from_pieces(pieces, cues)


def test_the_three_arrays_are_parallel_and_ordered() -> None:
    """Equal lengths, ``starts[i] <= ends[i]``, and both monotonic — the plan's shape contract."""

    cues, characters = _record("今天讲两件事。明天我们接着讲第三件事。")

    assert len(cues) >= 2, "the fixture must produce more than one cue to be worth asserting on"
    assert len(characters["starts"]) == len(characters["text"])
    assert len(characters["ends"]) == len(characters["text"])
    assert all(start <= end for start, end in zip(characters["starts"], characters["ends"]))
    assert characters["starts"] == sorted(characters["starts"])
    assert characters["ends"] == sorted(characters["ends"])


def test_the_record_covers_the_cue_text_character_for_character() -> None:
    """The completeness condition: the record's text **is** the published cue text, in order."""

    cues, characters = _record("今天讲两件事。明天我们接着讲第三件事。")

    assert characters["text"] == "".join(str(cue["text"]) for cue in cues)
    # The record is character-granular, not cue-granular: it is strictly finer unless the
    # transcript is a single character.
    assert len(characters["text"]) >= len(cues)


def test_a_character_the_cue_text_does_not_carry_is_refused() -> None:
    """The projection is checked, not assumed: a record for other text cannot be produced."""

    pieces = [{"text": "你好", "start": 0.0, "end": 1.0}]

    with pytest.raises(ValueError, match="not a projection"):
        _module_asr_alignment._characters_from_pieces(pieces, [{"start": 0.0, "end": 1.0, "text": "再见"}])


def test_piece_text_the_cue_text_drops_is_refused_when_it_is_not_whitespace() -> None:
    """Only whitespace may be dropped; anything else means the two streams disagree."""

    pieces = [{"text": "你好", "start": 0.0, "end": 1.0}]

    with pytest.raises(ValueError, match="not a projection"):
        _module_asr_alignment._characters_from_pieces(pieces, [{"start": 0.0, "end": 1.0, "text": "你好吗"}])


# ---------------------------------------------------------------------------------------
# The writer: the integrity gate is a product requirement, not a test's opinion.
# ---------------------------------------------------------------------------------------


def _entry(bvid: str, cid: int = 7) -> dict:
    ident = page_identity(bvid, 0, cid)
    return {
        "bvid": ident.bvid,
        "work_id": ident.work_id,
        "page_index": 0,
        "cid": cid,
        "title": "字符级",
        "pubdate_str": "2026-01-02",
        "duration_s": 5,
    }


def test_write_archive_publishes_the_character_record(tmp_root) -> None:
    """The ASR path records the aligner's per-character instants beside its cues."""

    from pathlib import Path

    tmp_path = Path(tmp_root)
    cues, characters = _record("今天讲两件事。明天我们接着讲第三件事。")

    paths = write_archive(tmp_path, _entry("BV1chars"), cues, source="asr", characters=characters)
    raw = json.loads((tmp_path / paths["raw_path"]).read_text(encoding="utf-8"))

    assert raw["schema"] == "archive-raw-v2"
    assert raw["characters"]["text"] == "".join(str(segment["text"]) for segment in raw["segments"])
    assert raw["characters"] == characters


def test_the_writer_refuses_a_record_that_contradicts_its_segments(tmp_root) -> None:
    """The refusal the plan makes a product requirement: disagree, and nothing is published.

    Deliberately unequal — the record claims a transcript the segments do not carry — so the gate
    is observed firing rather than described.
    """

    from pathlib import Path

    tmp_path = Path(tmp_root)
    entry = _entry("BV1refuse")
    segments = [{"start": 0.0, "end": 1.0, "text": "甲乙"}]

    with pytest.raises(ValueError, match="does not match the segments"):
        write_archive(
            tmp_path, entry, segments, source="asr",
            characters={"text": "丙丁", "starts": [0.0, 0.5], "ends": [0.5, 1.0]},
        )

    # Refused means **not written**: no bundle, hence no marker either.
    assert not (
        tmp_path / "transcripts" / f"{entry['work_id'].replace(':', '.')}" / "bundle.raw.json"
    ).exists()


def test_the_writer_refuses_a_record_with_a_missing_instant(tmp_root) -> None:
    """One instant per character: a shorter array is a claim the record cannot support."""

    from pathlib import Path

    with pytest.raises(ValueError, match="one instant per character"):
        write_archive(
            Path(tmp_root), _entry("BV1short"), [{"start": 0.0, "end": 1.0, "text": "甲乙"}],
            source="asr", characters={"text": "甲乙", "starts": [0.0], "ends": [0.5, 1.0]},
        )


def test_the_writer_refuses_a_record_with_inverted_instants(tmp_root) -> None:
    """A character cannot end before it starts.

    The plan states three shape invariants; only the length one was enforced until L2 review.
    An inverted interval describes a transcript that never happened, and a re-tiler indexing it
    would emit cues that run backwards.
    """

    from pathlib import Path

    with pytest.raises(ValueError, match="must not be inverted"):
        write_archive(
            Path(tmp_root), _entry("BV1inverted"), [{"start": 0.0, "end": 1.0, "text": "甲乙"}],
            source="asr",
            characters={"text": "甲乙", "starts": [0.9, 0.1], "ends": [0.2, 0.4]},
        )


def test_the_writer_refuses_a_record_whose_instants_run_backwards(tmp_root) -> None:
    """Instants advance with the transcript; a decreasing array is time running backwards."""

    from pathlib import Path

    with pytest.raises(ValueError, match="must not run backwards"):
        write_archive(
            Path(tmp_root), _entry("BV1backwards"), [{"start": 0.0, "end": 1.0, "text": "甲乙"}],
            source="asr",
            characters={"text": "甲乙", "starts": [5.0, 2.0], "ends": [5.5, 2.5]},
        )


def test_a_record_may_give_two_characters_the_same_instant(tmp_root) -> None:
    """Non-decreasing, not strictly increasing: a zero-length span is legitimate.

    Pinned so the monotonicity check above cannot be tightened into a false refusal — an aligner
    may hand two characters the same instant, and that is a shape the writer must still publish.
    """

    from pathlib import Path

    paths = write_archive(
        Path(tmp_root), _entry("BV1tie"), [{"start": 0.0, "end": 1.0, "text": "甲乙"}],
        source="asr",
        characters={"text": "甲乙", "starts": [0.0, 0.0], "ends": [0.5, 0.5]},
    )

    raw = json.loads(Path(tmp_root, paths["raw_path"]).read_text(encoding="utf-8"))

    assert raw["characters"]["starts"] == [0.0, 0.0]


def test_an_empty_record_is_the_same_as_no_record(tmp_root) -> None:
    """A zero-character transcription must not claim ``archive-raw-v2``.

    ``asr.py`` skips a chunk that decoded to nothing, so an all-empty record is reachable.  It is
    truthy as a dict, so it used to publish ``characters`` *and* the marker on a bundle that carries
    no character timings — leaving the two ASR shapes disagreeing about what the marker means.
    ``None`` and an empty record must produce the same bundle.
    """

    from pathlib import Path

    tmp_path = Path(tmp_root)

    write_archive(
        tmp_path, _entry("BV1emptytrivial", 11), [], source="asr", characters=None,
    )
    write_archive(
        tmp_path, _entry("BV1emptyrecord", 12), [], source="asr",
        characters={"text": "", "starts": [], "ends": []},
    )

    def _raw(bvid: str, cid: int) -> dict:
        stem = f"{bvid}.p0"
        return json.loads(
            (tmp_path / "transcripts" / stem / "bundle.raw.json").read_text(encoding="utf-8")
        )

    raw_none = _raw("BV1emptytrivial", 11)
    raw_empty = _raw("BV1emptyrecord", 12)

    assert "characters" not in raw_none and "characters" not in raw_empty
    assert "schema" not in raw_none and "schema" not in raw_empty
    assert set(raw_none) == set(raw_empty)


def test_a_subtitle_derived_raw_carries_no_characters(tmp_root) -> None:
    """Negative control: the caption path has no character timings, so it must not claim any.

    Both shapes a caption-derived bundle is published from are exercised: the reader-derived path
    (``raw=None``, which is how the four published route1 artifacts were written) and the harvested
    caption document passed through as ``raw``.  Even when a *record* is handed to the writer, a
    non-ASR source does not publish it: those products were never aligned against the audio.
    """

    from pathlib import Path

    tmp_path = Path(tmp_root)
    record = {"text": "字幕一句话", "starts": [0.0] * 5, "ends": [2.0] * 5}
    segments = [{"start": 0.0, "end": 2.0, "text": "字幕一句话"}]

    # The reader-derived shape: what `bili-asr publish` writes today.
    reader_paths = write_archive(
        tmp_path, _entry("BV1subtitle"), segments, source="subtitle-ai", characters=record,
    )
    reader_raw = json.loads(
        (tmp_path / reader_paths["raw_path"]).read_text(encoding="utf-8")
    )
    assert "characters" not in reader_raw
    assert "schema" not in reader_raw
    assert reader_raw["source"] == "subtitle-ai"
    assert reader_raw["segments"] == segments

    # The harvested-document shape: the caption document passes through untouched.
    caption_document = {"body": [{"from": 0.0, "to": 2.0, "content": "字幕一句话"}]}
    document_paths = write_archive(
        tmp_path, _entry("BV1subtitle2"), segments, source="subtitle-ai",
        raw=caption_document, characters=record,
    )
    assert json.loads(
        (tmp_path / document_paths["raw_path"]).read_text(encoding="utf-8")
    ) == caption_document


def test_a_subtitle_derived_raw_publishes_its_caption_document_untouched(tmp_root) -> None:
    """The caption path's own shape is unchanged by this field (its document passes through)."""

    from pathlib import Path

    tmp_path = Path(tmp_root)
    caption_document = {"body": [{"from": 0.0, "to": 2.0, "content": "字幕一句话"}]}

    paths = write_archive(
        tmp_path, _entry("BV1passthrough"), [{"start": 0.0, "end": 2.0, "text": "字幕一句话"}],
        source="subtitle-cc", raw=caption_document,
    )
    raw = json.loads((tmp_path / paths["raw_path"]).read_text(encoding="utf-8"))

    assert raw == caption_document


def test_the_asr_path_without_a_record_stays_writable(tmp_root) -> None:
    """A caller that holds no aligner output publishes the pre-field shape, not a fabricated one.

    This is the seam every existing ASR caller sits on until it is taught to pass the record, and
    it is what keeps an injected runner (a test double, an older boundary) from failing a write.
    """

    from pathlib import Path

    tmp_path = Path(tmp_root)
    paths = write_archive(
        tmp_path, _entry("BV1norecord"), [{"start": 0.0, "end": 1.0, "text": "无记录"}],
        source="asr",
    )
    raw = json.loads((tmp_path / paths["raw_path"]).read_text(encoding="utf-8"))

    assert "characters" not in raw
    assert "schema" not in raw
    assert raw["source"] == "asr"


# ---------------------------------------------------------------------------------------
# Backward compatibility: an artifact written before this field existed is still an artifact.
# ---------------------------------------------------------------------------------------


def test_an_old_artifact_without_characters_is_still_read_by_the_route_reader(tmp_root) -> None:
    """Negative control: the pre-field document still reads — missing ``characters`` is not an error.

    The document is written by hand in the exact shape the four published route2 artifacts have
    (``segments`` / ``source`` / ``provenance``, no ``characters``, no ``schema``), and then read
    back through the reader the product actually uses.
    """

    from pathlib import Path

    from bili_asr.proofread import read_asr_route_ms

    tmp_path = Path(tmp_root)
    bundle = tmp_path / "transcripts" / "BV1old.p0"
    bundle.mkdir(parents=True)
    (bundle / "bundle.raw.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.32, "end": 5.76, "text": "旧产物第一句。"},
                    {"start": 5.76, "end": 9.1, "text": "旧产物第二句。"},
                ],
                "source": "asr",
                "provenance": {"model_name": "Qwen/Qwen3-ASR-1.7B-hf", "device": "cuda"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert read_asr_route_ms(tmp_path, "BV1old", 0) == [
        (320, 5760, "旧产物第一句。"),
        (5760, 9100, "旧产物第二句。"),
    ]


def test_an_old_artifact_is_byte_identical_when_no_record_is_passed(tmp_root) -> None:
    """The change is additive: with no record the raw document is what it has always been."""

    from pathlib import Path

    tmp_path = Path(tmp_root)
    segments = [{"start": 0.0, "end": 1.0, "text": "你好。"}]
    provenance = {"model_name": "Qwen/Qwen3-ASR-1.7B-hf", "device": "cpu"}

    paths = write_archive(
        tmp_path, _entry("BV1identical"), segments, source="asr", asr_provenance=provenance,
    )
    raw = json.loads((tmp_path / paths["raw_path"]).read_text(encoding="utf-8"))

    assert raw == {"segments": segments, "source": "asr", "provenance": provenance}


# ---------------------------------------------------------------------------------------
# The whole chain: a runner transcription is what the published record describes.
# ---------------------------------------------------------------------------------------


def test_a_real_transcription_reaches_the_published_raw(tmp_root, monkeypatch) -> None:
    """``transcribe`` → ``characters()`` → ``write_archive``: the wiring, not just the parts.

    The model pair is replaced and the audio read is patched. Numpy and soundfile
    are required development dependencies; everything between —
    ``_thread_text``, ``_aligned_cues``, the record and the writer's gate — is the real code path.
    """

    import numpy as np
    import soundfile
    from pathlib import Path

    from bili_asr.page_identity import page_identity

    text = "今天讲两件事。明天我们接着讲第三件事。"
    units = _units(text)
    samples = np.zeros(_module_asr_constants.SAMPLE_RATE * 3, dtype="float32")
    monkeypatch.setattr(soundfile, "read", lambda *args, **kwargs: (samples, _module_asr_constants.SAMPLE_RATE))
    monkeypatch.setattr(soundfile, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(_module_asr_audio, "_materialize_input", lambda path: (path, None))

    runner = _module_asr_runner.ASRRunner(_module_asr_config.ASRConfig(model_name="Qwen/Qwen3-ASR-1.7B-hf", device="cpu"))
    from types import SimpleNamespace

    models = SimpleNamespace(model=SimpleNamespace(dtype=None), aligner=SimpleNamespace(dtype=None))
    monkeypatch.setattr(runner, "_get_models", lambda: models)
    monkeypatch.setattr(runner, "_transcribe_chunk", lambda models, path, **kwargs: (text, "Chinese"))
    monkeypatch.setattr(runner, "_align_chunk", lambda models, path, chunk_text, language: units)

    assert runner.characters() is None, "no run yet: the record is not invented"

    segments = runner.transcribe("/nonexistent/one.wav")
    characters = _module_asr_coverage.characters_of(runner)

    assert characters is not None
    assert characters["text"] == "".join(str(segment["text"]) for segment in segments)
    assert len(characters["starts"]) == len(characters["text"]) == len(characters["ends"])

    tmp_path = Path(tmp_root)
    ident = page_identity("BV1chain", 0, 6)
    entry = {
        "bvid": ident.bvid, "work_id": ident.work_id, "page_index": 0, "cid": 6,
        "title": "chain", "pubdate_str": "2026-01-02", "duration_s": 5,
    }
    paths = write_archive(
        tmp_path, entry, segments, source="asr", characters=characters,
        asr_provenance=runner.provenance(),
    )
    raw = json.loads((tmp_path / paths["raw_path"]).read_text(encoding="utf-8"))

    assert raw["schema"] == "archive-raw-v2"
    assert raw["characters"] == characters
    assert raw["characters"]["text"] == "".join(
        str(segment["text"]) for segment in raw["segments"]
    )
