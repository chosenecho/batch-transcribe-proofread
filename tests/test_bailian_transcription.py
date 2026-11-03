"""Cloud calls preserve raw results, timestamps and paid-attempt checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from bili_asr.asr import bailian as batch


def result(text="测试文本", start=0, end=1000):
    return {
        "output": {
            "text": text,
            "sentences": [
                {
                    "begin_time": start,
                    "end_time": end,
                    "sentence_end": True,
                    "text": text,
                    "speaker_id": 0,
                }
            ],
        },
        "usage": {"input_tokens": 4},
    }


def test_nested_live_shape_and_chunk_offsets():
    text, cues = batch.read_result(
        {"output": result()["output"]}, offset=240, duration=2
    )
    assert text == "测试文本" and cues[0]["start"] == 240 and cues[0]["end"] == 241
    assert (
        batch.read_result(
            {"output": {"output": result()["output"]}}, offset=0, duration=2
        )[0]
        == text
    )


@pytest.mark.parametrize("start,end", [(True, 2), (-1, 1000), (2000, 1000), (0, 3000)])
def test_invalid_timestamps_are_never_sorted_or_clamped(start, end):
    with pytest.raises(ValueError, match="unrepresentable"):
        batch.read_result(result(start=start, end=end), offset=0, duration=2)


def test_empty_transcript_requires_review():
    with pytest.raises(ValueError, match="empty_transcript"):
        batch.read_result(
            {"output": {"text": "", "sentences": []}}, offset=0, duration=2
        )


@responses.activate
def test_exact_model_payload_and_redacted_provider_error(tmp_path):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fixture")
    responses.post(
        batch.ENDPOINT,
        json={"code": "AccessDenied", "message": "PRIVATE_SECRET"},
        status=403,
    )
    with pytest.raises(batch.CloudError, match="^AccessDenied$"):
        batch.call_cloud(wav, "PRIVATE_KEY")
    sent = json.loads(responses.calls[0].request.body)
    assert sent["model"] == "qwen-audio-3.1-asr-flash"
    assert sent["parameters"]["speaker_diarization_enabled"] is True
    assert responses.calls[0].request.headers["Authorization"] == "Bearer PRIVATE_KEY"
    assert "PRIVATE_KEY" not in str(sent)


@pytest.fixture
def audio_and_mock_encoder(tmp_path, monkeypatch):
    audio = tmp_path / "source.opus"
    audio.write_bytes(b"original fixture audio")
    monkeypatch.setattr(batch, "probe_duration", lambda path: 241)

    def encode(args, **kwargs):
        Path(args[-1]).write_bytes(b"encoded fixture")

    monkeypatch.setattr(batch.subprocess, "run", encode)
    return audio, tmp_path / "output"


def test_completed_chunks_resume_without_repaying(audio_and_mock_encoder):
    audio, output = audio_and_mock_encoder
    calls = []

    def cloud(path, key):
        calls.append(path)
        return result()

    arguments = {
        "api_key": "PRIVATE",
        "expected_sha256": batch.sha256(audio),
        "cloud": cloud,
    }
    batch.transcribe(audio, output, **arguments)
    again = batch.transcribe(audio, output, **arguments)
    assert len(calls) == 2 and again["chunks"] == 2
    transcript = json.loads((output / "transcript.json").read_text("utf-8"))
    assert [cue["start"] for cue in transcript["segments"]] == [0, 240]
    assert "PRIVATE" not in (output / "request-config.json").read_text("utf-8")
    assert (output / "complete.json").is_file()


def test_failed_paid_request_needs_explicit_retry_and_preserves_attempt(
    audio_and_mock_encoder,
):
    audio, output = audio_and_mock_encoder

    def fail(path, key):
        raise batch.CloudError("transport_outcome_unknown")

    arguments = {"api_key": "PRIVATE", "expected_sha256": batch.sha256(audio)}
    with pytest.raises(batch.CloudError, match="transport_outcome_unknown"):
        batch.transcribe(audio, output, cloud=fail, **arguments)
    with pytest.raises(batch.CloudError, match="previous_request_needs_review"):
        batch.transcribe(
            audio,
            output,
            cloud=lambda *args: pytest.fail("must not replay"),
            **arguments,
        )
    batch.transcribe(
        audio, output, cloud=lambda *args: result(), retry_failed=True, **arguments
    )
    attempts = list(output.glob("chunk-0000.attempt-*.json"))
    assert len(attempts) == 1
    assert (
        json.loads(attempts[0].read_text("utf-8"))["error_code"]
        == "transport_outcome_unknown"
    )


def test_input_hash_mismatch_refuses_cloud_request(audio_and_mock_encoder):
    audio, output = audio_and_mock_encoder
    with pytest.raises(ValueError, match="audio_sha256_mismatch"):
        batch.transcribe(
            audio,
            output,
            api_key="PRIVATE",
            expected_sha256="wrong",
            cloud=lambda *args: pytest.fail("must not request"),
        )


def test_provider_no_words_retains_error_evidence_and_marks_review(
    audio_and_mock_encoder,
):
    audio, output = audio_and_mock_encoder
    calls = []

    def cloud(*args):
        calls.append(1)
        if len(calls) == 2:
            raise batch.CloudError(
                "CLIENT_ERROR",
                request_id="request-123",
                status=400,
                detail="ASR_RESPONSE_HAVE_NO_WORDS",
            )
        return result()

    kwargs = {
        "api_key": "PRIVATE",
        "expected_sha256": batch.sha256(audio),
        "cloud": cloud,
    }
    assert batch.transcribe(audio, output, **kwargs)["requires_review"]
    batch.transcribe(audio, output, **kwargs)
    assert len(calls) == 2
    raw = json.loads((output / "chunk-0001.json").read_text("utf-8"))
    assert raw["state"] == "failed" and raw["provider_request_id"] == "request-123"
    transcript = json.loads((output / "transcript.json").read_text("utf-8"))
    assert transcript["untranscribed_intervals"] == [
        {
            "start": 240,
            "end": 241,
            "reason": "provider_returned_no_words",
            "evidence": "chunk-0001.json",
        }
    ]


def test_bad_provider_timestamps_preserve_text_without_exporting_partial_subtitles(
    audio_and_mock_encoder,
):
    audio, output = audio_and_mock_encoder
    calls = []

    def cloud(*args):
        calls.append(1)
        return result(end=0) if len(calls) == 1 else result()

    batch.transcribe(
        audio,
        output,
        api_key="PRIVATE",
        expected_sha256=batch.sha256(audio),
        cloud=cloud,
    )
    assert len(calls) == 2
    transcript = json.loads((output / "transcript.json").read_text("utf-8"))
    assert transcript["requires_review"] and not transcript["timed_exports_available"]
    assert transcript["text"] == "测试文本\n测试文本"
    assert not (output / "transcript.srt").exists()
    assert not (output / "transcript.vtt").exists()
    assert json.loads((output / "chunk-0000.json").read_text("utf-8"))[
        "response"
    ] == result(end=0)


def test_explicit_retry_can_reprocess_no_words_without_losing_evidence(
    audio_and_mock_encoder,
):
    audio, output = audio_and_mock_encoder

    def empty(*args):
        raise batch.CloudError("CLIENT_ERROR", detail="ASR_RESPONSE_HAVE_NO_WORDS")

    kwargs = {"api_key": "PRIVATE", "expected_sha256": batch.sha256(audio)}
    assert batch.transcribe(audio, output, cloud=empty, **kwargs)["requires_review"]
    assert not batch.transcribe(
        audio, output, cloud=lambda *a: result(), retry_failed=True, **kwargs
    )["requires_review"]
    assert len(list(output.glob("chunk-*.attempt-*.json"))) == 2


def test_interrupted_retry_cannot_leave_old_complete_marker(audio_and_mock_encoder):
    audio, output = audio_and_mock_encoder

    def empty(*args):
        raise batch.CloudError("CLIENT_ERROR", detail="ASR_RESPONSE_HAVE_NO_WORDS")

    kwargs = {"api_key": "PRIVATE", "expected_sha256": batch.sha256(audio)}
    batch.transcribe(audio, output, cloud=empty, **kwargs)
    assert (output / "complete.json").exists()

    def fail(*args):
        raise batch.CloudError("transport_outcome_unknown")

    with pytest.raises(batch.CloudError, match="transport_outcome_unknown"):
        batch.transcribe(audio, output, cloud=fail, retry_failed=True, **kwargs)
    assert not (output / "complete.json").exists()


@responses.activate
@pytest.mark.parametrize(
    "document", [[], None, {"code": "PRIVATE_KEY", "message": "PRIVATE_KEY"}]
)
def test_malformed_or_credential_echoing_provider_error_is_bounded(tmp_path, document):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fixture")
    responses.post(batch.ENDPOINT, body=json.dumps(document), status=403)
    with pytest.raises(batch.CloudError) as caught:
        batch.call_cloud(wav, "PRIVATE_KEY")
    assert str(caught.value) in {"response_shape", "provider_error"}
    assert "PRIVATE_KEY" not in str(caught.value.detail)


def test_all_no_words_does_not_leave_empty_or_stale_subtitles(audio_and_mock_encoder):
    audio, output = audio_and_mock_encoder
    output.mkdir()
    (output / "transcript.srt").write_text("stale derived export", encoding="utf-8")
    (output / "transcript.vtt").write_text("WEBVTT\n\n", encoding="utf-8")

    def cloud(*args):
        raise batch.CloudError("CLIENT_ERROR", detail="ASR_RESPONSE_HAVE_NO_WORDS")

    batch.transcribe(
        audio,
        output,
        api_key="PRIVATE",
        expected_sha256=batch.sha256(audio),
        cloud=cloud,
    )
    transcript = json.loads((output / "transcript.json").read_text("utf-8"))
    assert transcript["requires_review"] and not transcript["timed_exports_available"]
    assert transcript["text"] == "" and len(transcript["untranscribed_intervals"]) == 2
    assert not (output / "transcript.srt").exists()
    assert not (output / "transcript.vtt").exists()
