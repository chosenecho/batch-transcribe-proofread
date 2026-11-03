"""Transcribe local audio with Qwen Audio 3.1 using a DashScope API key.

Cloud results are separate exports, never mislabeled as local-ASR publications.
Paid requests are checkpointed; interrupted/failed requests require explicit retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from bili_asr.cues import segments_to_srt, segments_to_vtt
from bili_asr.file_ops import exclusive_lock

MODEL = "qwen-audio-3.1-asr-flash"
ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
CHUNK_SECONDS = 240


class CloudError(RuntimeError):
    """Only bounded, credential-free codes escape the request boundary."""

    def __init__(self, code, *, request_id=None, status=None, detail=None):
        super().__init__(code)
        self.request_id, self.status, self.detail = request_id, status, detail


def write_json(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pending.replace(path)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe_duration(path):
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    value = float(json.loads(p.stdout)["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_audio_duration")
    return value


def call_cloud(wav, api_key):
    if not api_key:
        raise CloudError("missing_api_key")
    body = {
        "model": MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "data:audio/wav;base64,"
                                + base64.b64encode(wav.read_bytes()).decode("ascii")
                            },
                        }
                    ],
                }
            ]
        },
        "parameters": {
            "format": "wav",
            "sample_rate": "16000",
            "speaker_diarization_enabled": True,
        },
    }
    try:
        with requests.post(
            ENDPOINT,
            headers={
                "Authorization": "Bearer " + api_key,
                "X-DashScope-SSE": "disable",
            },
            json=body,
            timeout=(15, 180),
            allow_redirects=False,
            stream=True,
        ) as response:
            data = bytearray()
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > 16 * 1024 * 1024:
                    raise CloudError("response_budget_exceeded")
            document = json.loads(data)
            if response.status_code != 200:
                code = document.get("code", "http_" + str(response.status_code))
                if (
                    not isinstance(code, str)
                    or api_key in code
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", code)
                ):
                    code = "provider_error"
                detail = str(document.get("message", "")).replace(api_key, "[REDACTED]")
                detail = re.sub(r"https?://\S+", "[URL]", detail)[:500]
                raise CloudError(
                    code,
                    request_id=document.get("request_id"),
                    status=response.status_code,
                    detail=detail,
                )
            if not isinstance(document, dict) or "output" not in document:
                raise CloudError("response_shape")
            return document
    except requests.RequestException as exc:
        # A timeout may already have incurred a charge. Never auto-replay it.
        detail = str(exc).replace(api_key, "[REDACTED]")
        detail = re.sub(r"https?://\S+", "[URL]", detail)[:500]
        raise CloudError(
            "transport_outcome_unknown", detail=type(exc).__name__ + ": " + detail
        ) from None
    except (json.JSONDecodeError, UnicodeError, AttributeError):
        raise CloudError("response_shape") from None


def read_result(document, *, offset, duration):
    output = document.get("output", {})
    # Both direct and gateway-wrapped response shapes exist in live responses.
    if not isinstance(output, dict):
        raise ValueError("response_shape")  # noqa: TRY004 - serialized provider data
    if "text" not in output and isinstance(output.get("output"), dict):
        output = output["output"]
    text = output.get("text")
    sentences = output.get("sentences")
    if not isinstance(text, str) or not isinstance(sentences, list):
        raise ValueError("response_missing_full_transcript")  # noqa: TRY004
    if not text.strip():
        raise ValueError("empty_transcript_needs_review")
    cues, previous = [], -1
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ValueError("unrepresentable_provider_timestamps")  # noqa: TRY004
        start, end, value = (
            sentence.get("begin_time"),
            sentence.get("end_time"),
            sentence.get("text"),
        )
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or start < previous
            or end > round(duration * 1000) + 100
            or not isinstance(value, str)
            or not value.strip()
            or sentence.get("sentence_end") is not True
        ):
            raise ValueError("unrepresentable_provider_timestamps")
        cues.append(
            {
                "start": offset + start / 1000,
                "end": offset + end / 1000,
                "text": value,
                "speaker_id": sentence.get("speaker_id"),
                "chunk_offset_seconds": offset,
            }
        )
        previous = start
    if text.strip() and not cues:
        raise ValueError("response_missing_timestamps")
    return text, cues


def transcribe(
    audio,
    destination,
    *,
    api_key,
    expected_sha256,
    retry_failed=False,
    cloud=call_cloud,
):
    audio, destination = Path(audio), Path(destination)
    actual_sha = sha256(audio)
    if actual_sha != expected_sha256:
        raise ValueError("audio_sha256_mismatch")
    duration = probe_duration(audio)
    destination.mkdir(parents=True, exist_ok=True)
    fingerprint = {
        "model": MODEL,
        "audio_sha256": actual_sha,
        "chunk_seconds": CHUNK_SECONDS,
        "speaker_diarization_enabled": True,
        "sample_rate": 16000,
        "version": 1,
    }
    with exclusive_lock(destination / "transcript"):
        manifest = destination / "request-config.json"
        if manifest.exists() and json.loads(manifest.read_text("utf-8")) != fingerprint:
            raise ValueError("existing_output_has_different_inputs")
        write_json(manifest, fingerprint)
        # A resumed attempt is incomplete until every chunk has been checked.
        (destination / "complete.json").unlink(missing_ok=True)
        texts, cues, usage, gaps = [], [], [], []
        for index in range(math.ceil(duration / CHUNK_SECONDS)):
            offset = index * CHUNK_SECONDS
            length = min(CHUNK_SECONDS, duration - offset)
            checkpoint = destination / f"chunk-{index:04d}.json"
            if checkpoint.exists():
                cached = json.loads(checkpoint.read_text("utf-8"))
                if cached["state"] == "succeeded":
                    document = cached["response"]
                elif (
                    cached.get("error_detail") == "ASR_RESPONSE_HAVE_NO_WORDS"
                    and not retry_failed
                ):
                    gaps.append(
                        {
                            "start": offset,
                            "end": offset + length,
                            "reason": "provider_returned_no_words",
                            "evidence": checkpoint.name,
                        }
                    )
                    continue
                elif not retry_failed:
                    raise CloudError("previous_request_needs_review")
                else:
                    checkpoint.rename(
                        checkpoint.with_name(
                            f"{checkpoint.stem}.attempt-{time.time_ns()}.json"
                        )
                    )
                    document = None
            else:
                document = None
            if document is None:
                with tempfile.TemporaryDirectory(
                    prefix=".bailian-audio-", dir=destination
                ) as staging:
                    wav = Path(staging) / "input.wav"
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-ss",
                            str(offset),
                            "-i",
                            str(audio),
                            "-t",
                            str(length),
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            "-c:a",
                            "pcm_s16le",
                            str(wav),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=120,
                    )
                    record = {
                        "state": "request_started",
                        "offset_seconds": offset,
                        "duration_seconds": length,
                        "started_at": int(time.time()),
                    }
                    write_json(checkpoint, record)
                    try:
                        document = cloud(wav, api_key)
                    except CloudError as exc:
                        write_json(
                            checkpoint,
                            {
                                **record,
                                "state": "failed",
                                "error_code": str(exc),
                                "provider_request_id": exc.request_id,
                                "http_status": exc.status,
                                "error_detail": exc.detail,
                            },
                        )
                        if exc.detail != "ASR_RESPONSE_HAVE_NO_WORDS":
                            raise
                        gaps.append(
                            {
                                "start": offset,
                                "end": offset + length,
                                "reason": "provider_returned_no_words",
                                "evidence": checkpoint.name,
                            }
                        )
                        continue
                    write_json(
                        checkpoint,
                        {**record, "state": "succeeded", "response": document},
                    )
            # Raw provider evidence is durable before validating/rendering it.
            try:
                text, current = read_result(document, offset=offset, duration=length)
            except ValueError as exc:
                if str(exc) != "unrepresentable_provider_timestamps":
                    raise
                original = document["output"]
                if "text" not in original:
                    original = original["output"]
                text, current = original["text"], []
                gaps.append(
                    {
                        "start": offset,
                        "end": offset + length,
                        "reason": "unrepresentable_provider_timestamps",
                        "evidence": checkpoint.name,
                        "transcript_text_available": True,
                    }
                )
            texts.append(text)
            cues.extend(current)
            usage.append(document.get("usage", {}))
            print(
                f"chunk {index + 1}/{math.ceil(duration / CHUNK_SECONDS)} saved",
                flush=True,
            )
        timing_valid = bool(cues) and not any(
            gap["reason"] == "unrepresentable_provider_timestamps" for gap in gaps
        )
        vtt = segments_to_vtt(cues) if timing_valid else None
        result = {
            **fingerprint,
            "source": "asr-cloud",
            "duration_seconds": duration,
            "segments": cues,
            "usage": usage,
            "speaker_ids_are_chunk_local": True,
            "untranscribed_intervals": gaps,
            "requires_review": bool(gaps),
            "timed_exports_available": timing_valid,
            "text": "\n".join(texts),
        }
        write_json(destination / "transcript.json", result)
        (destination / "transcript.txt").write_text(
            result["text"] + "\n", encoding="utf-8"
        )
        if timing_valid:
            (destination / "transcript.srt").write_text(
                segments_to_srt(cues), encoding="utf-8"
            )
            (destination / "transcript.vtt").write_text(vtt, encoding="utf-8")
        else:
            # These are derived exports; never leave stale/empty subtitles behind.
            (destination / "transcript.srt").unlink(missing_ok=True)
            (destination / "transcript.vtt").unlink(missing_ok=True)
        write_json(
            destination / "complete.json",
            {
                "model": MODEL,
                "audio_sha256": actual_sha,
                "chunks": math.ceil(duration / CHUNK_SECONDS),
                "segments": len(cues),
                "requires_review": bool(gaps),
                "finished_at": int(time.time()),
            },
        )
        return {
            "chunks": math.ceil(duration / CHUNK_SECONDS),
            "segments": len(cues),
            "duration_seconds": duration,
            "requires_review": bool(gaps),
        }
