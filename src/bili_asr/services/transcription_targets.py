"""Offline selection parsing for already-imported transcription audio."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from bili_asr.platform_identity import ContentRef
from bili_asr.source_identity import source_url, youtube_ref

_INPUT_BYTES = 16 * 1024 * 1024
_INPUT_ROWS = 10_000


@dataclass(frozen=True)
class YoutubeTarget:
    ref: ContentRef
    reference_bvids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"platform": "youtube", "external_video_id": self.ref.external_video_id,
                "url": source_url(self.ref), "reference_bvids": list(self.reference_bvids)}


def load_youtube_targets(*, input_path: Path | None = None,
                         videos: list[str] | None = None) -> tuple[YoutubeTarget, ...]:
    """Validate the whole input before network or archive mutations; dedupe by ID."""
    if (input_path is None) == (videos is None):
        raise ValueError("choose --input or --youtube")
    rows = videos
    if input_path is not None:
        with input_path.open("rb") as stream:
            data = stream.read(_INPUT_BYTES + 1)
        if len(data) > _INPUT_BYTES:
            raise ValueError("YouTube input exceeds 16 MiB")
        text = data.decode("utf-8-sig")
        suffix = input_path.suffix.lower()
        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            if not set(reader.fieldnames or ()) & {"youtube_url", "youtubeUrl", "url", "youtube_id"}:
                raise ValueError("CSV needs a youtube_url, youtubeUrl, url or youtube_id column")
            try:
                rows = list(reader)
            except csv.Error:
                raise ValueError("YouTube CSV could not be parsed") from None
        elif suffix == ".json":
            rows = json.loads(text)
            if not isinstance(rows, list):
                raise ValueError("YouTube JSON input must be an array (including cola videos.json)")
        elif suffix == ".txt":
            rows = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        else:
            raise ValueError("YouTube input must be CSV, JSON or TXT")
    if not isinstance(rows, list) or len(rows) > _INPUT_ROWS:
        raise ValueError("YouTube input must contain at most 10000 rows")
    targets: dict[ContentRef, list[str]] = {}
    for number, row in enumerate(rows, 1):
        if isinstance(row, str):
            value, bvid = row, None
        elif isinstance(row, dict):
            # An explicit empty YouTube mapping must not fall back to a Bilibili URL.
            keys = ("youtube_url", "youtubeUrl", "youtube_id", "url")
            key = next((key for key in keys if key in row), None)
            if key is None:
                raise ValueError(f"YouTube input row {number} has no URL field")
            value = row[key]
            if value is None or value == "":
                continue
            bvid = row.get("bvid", row.get("id"))
        else:
            raise TypeError(f"YouTube input row {number} must be a URL or object")
        try:
            ref = youtube_ref(value.strip() if isinstance(value, str) else value)
        except (ValueError, TypeError):
            # Do not echo arbitrary input (which could contain credentials).
            raise ValueError(f"YouTube input row {number} is not a valid single-video ID/URL") from None
        labels = targets.setdefault(ref, [])
        if isinstance(bvid, str) and bvid.startswith("BV") and bvid.isascii() and bvid.isalnum() and bvid not in labels:
            labels.append(bvid)
    if not targets:
        raise ValueError("YouTube input contains no mapped videos")
    return tuple(YoutubeTarget(ref, tuple(labels)) for ref, labels in targets.items())
