"""Provider and CLI failures stay bounded and do not silently replay paid work."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests
import responses

from bili_asr.asr import bailian


@pytest.mark.parametrize("duration", ["120.5", "0", "-1", "NaN", "Infinity"])
def test_ffprobe_duration_is_validated(monkeypatch, duration):
    def probe(args, **kwargs):
        assert args[0] == "ffprobe" and kwargs["timeout"] == 30
        return SimpleNamespace(stdout=json.dumps({"format": {"duration": duration}}))

    monkeypatch.setattr(bailian.subprocess, "run", probe)
    if duration == "120.5":
        assert bailian.probe_duration("input.mp3") == 120.5
    else:
        with pytest.raises(ValueError, match="invalid_audio_duration"):
            bailian.probe_duration("input.mp3")


def test_missing_key_does_not_read_audio(tmp_path):
    with pytest.raises(bailian.CloudError, match="missing_api_key"):
        bailian.call_cloud(tmp_path / "nonexistent.wav", "")


@responses.activate
@pytest.mark.parametrize(
    "document", [None, {}, {"output": {"text": "test", "sentences": []}}]
)
def test_success_response_envelope_is_validated(tmp_path, document):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fixture")
    responses.post(bailian.ENDPOINT, body=json.dumps(document), status=200)
    if isinstance(document, dict) and "output" in document:
        assert bailian.call_cloud(wav, "PRIVATE_KEY") == document
    else:
        with pytest.raises(bailian.CloudError, match="response_shape"):
            bailian.call_cloud(wav, "PRIVATE_KEY")


@responses.activate
def test_response_budget_stops_oversized_provider_output(tmp_path):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fixture")
    responses.post(bailian.ENDPOINT, body=b"x" * (16 * 1024 * 1024 + 1), status=200)
    with pytest.raises(bailian.CloudError, match="response_budget_exceeded"):
        bailian.call_cloud(wav, "PRIVATE_KEY")
    assert len(responses.calls) == 1


@responses.activate
def test_transport_timeout_is_not_retried_and_redacts_credentials(tmp_path):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fixture")
    responses.post(
        bailian.ENDPOINT,
        body=requests.Timeout("PRIVATE_KEY https://example.invalid/?token=PRIVATE_KEY"),
    )
    with pytest.raises(bailian.CloudError, match="transport_outcome_unknown") as caught:
        bailian.call_cloud(wav, "PRIVATE_KEY")
    assert len(responses.calls) == 1
    assert (
        "PRIVATE_KEY" not in caught.value.detail
        and "example.invalid" not in caught.value.detail
    )


@pytest.mark.parametrize(
    "output,error",
    [
        ([], "response_shape"),
        ({"text": "words"}, "response_missing_full_transcript"),
        ({"text": "words", "sentences": [None]}, "unrepresentable_provider_timestamps"),
        ({"text": "words", "sentences": []}, "response_missing_timestamps"),
    ],
)
def test_malformed_transcript_cannot_be_published(output, error):
    with pytest.raises(ValueError, match=error):
        bailian.read_result({"output": output}, offset=0, duration=2)
