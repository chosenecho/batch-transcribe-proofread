"""Conservative pre-call checks; never rewrite transcript evidence."""

from __future__ import annotations

import re
from collections import Counter

from bili_asr.workflow_errors import JobExecutionError


def _check_filler_only_source(base: dict) -> None:
    segments = base.get('segments', [])
    if len(segments) != 1:
        return
    segment = segments[0]
    text = re.sub(r'[\W_]+', '', segment['text'])
    duration = segment['end_ms'] - segment['start_ms']
    if duration >= 600_000 and len(text) >= 6 and set(text) == {chr(0x55ef)}:
        raise JobExecutionError('editorial_source_repetition', {
            'stage': 'source_quality', 'reason_code': 'long_filler_only_source',
            'transcript_id': base['transcript_id'], 'segment_ordinal': 0,
            'start_ms': segment['start_ms'], 'end_ms': segment['end_ms'],
            'characters': len(segment['text']), 'filler_count': len(text),
        })


def check_source_quality(prepared: dict, *, connection=None, run_id: str | None = None) -> None:
    """Check source timeline, repetition and persisted run admission before calls.

    A long segment or ordinary rhetorical repetition alone is insufficient.
    Thresholds deliberately target the observed multi-thousand-character bursts.
    A pure invocation measures frozen cues; paid execution additionally supplies
    the connection and producing run to verify measured coverage without guessing.
    """
    if prepared["snapshot"]["base"]["source_kind"] != "asr-local":
        return
    _check_filler_only_source(prepared['snapshot']['base'])
    from bili_asr.asr.admission import assess_output
    from bili_asr.storage.asr_quality import assess_transcript, require_admission

    base = prepared["snapshot"]["base"]
    cues = [{"start": item["start_ms"] / 1000, "end": item["end_ms"] / 1000, "text": item["text"]}
            for item in base["segments"]]
    structural = {**assess_output(cues), "transcript_id": base["transcript_id"]}
    if structural["status"] == "unusable":
        require_admission(structural, stage="source_quality")
    for ordinal, segment in enumerate(prepared["snapshot"]["base"]["segments"]):
        text = segment["text"]
        duration = max(1, segment["end_ms"] - segment["start_ms"])
        if len(text) < 1000 or len(text) * 1000 <= duration * 100:
            continue
        words = re.findall(r"[a-z]+|[\u4e00-\u9fff]", text[:100_000].casefold())
        counts = Counter(tuple(words[i:i + 4]) for i in range(max(0, len(words) - 3)))
        repeats = max(counts.values(), default=0)
        if repeats >= 24:
            raise JobExecutionError("editorial_source_repetition", {
                "stage": "source_quality", "reason_code": "dense_repeated_segment",
                "transcript_id": prepared["snapshot"]["base"]["transcript_id"],
                "segment_ordinal": ordinal, "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"], "characters": len(text),
                "repeated_fourgram_count": repeats,
                "minimum_characters": 1000, "density_limit_chars_per_second": 100,
                "minimum_repeated_fourgrams": 24,
            })
    if connection is not None:
        require_admission(assess_transcript(connection, base["transcript_id"], run_id=run_id), stage="source_quality")
