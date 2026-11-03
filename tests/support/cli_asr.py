from __future__ import annotations
import bili_asr.asr.errors as _module_asr_errors
import bili_asr.asr.runner as _module_asr_runner
import json
import os
import sys
import pytest
from bili_asr import asr as asr_mod
from bili_asr import bili_client as bc
from bili_asr.cli.main import main
from bili_asr.manifest import ManifestStore
from bili_asr.page_identity import artifact_stem, page_identity
from tests.support.audio import (
    AUDIO_BYTES,
    SPI_OK,
    STREAM_HOST,
    RouterTransport,
    nav_response,
    playurl_ok,
)
from tests.support.subtitles import SAMPLE_DOC, nav_ok, player_ok, sub_entry
from tests.conftest import reuse_line
import tests.support.asr_fakes as asr_fakes
from tests.support.archive_database import _seed_archive_database


def _stub_runner_model(monkeypatch, calls=None, released=None):
    """D2.5 seam: patch the module-level factory, not ``asr.transcribe``.

    The real ``_get_model`` path stays under test, so the counter a command reports is the one the
    production construction site produces.  Returns the list of construction kwargs, one entry per
    model set built.  ``calls`` collects the path of every recording the boundary opened — the model
    is handed a chunk file, so a row is identified at the read, not at the model.  ``released``
    records each ``ASRRunner.release()`` call, so a test can assert the invocation-scoped runner is
    handed back on every exit path.
    """

    constructions: list[dict] = []
    asr_fakes.install(monkeypatch, text="asr-text", constructions=constructions, reads=calls)

    if released is not None:
        real_release = _module_asr_runner.ASRRunner.release

        def recording_release(self):
            released.append(self)
            real_release(self)

        monkeypatch.setattr(_module_asr_runner.ASRRunner, "release", recording_release)

    return constructions


def _patch_cli(monkeypatch, transport=None):
    if transport is not None:
        monkeypatch.setattr(bc, "build_default_transport", lambda: transport)
    monkeypatch.setattr(bc, "default_sleeper", lambda: (lambda _seconds: None))


def _audio_transport():
    return RouterTransport(
        {
            "finger/spi": [SPI_OK],
            "nav": [nav_ok(), nav_response()],
            "player/wbi/v2": [player_ok([])],
            "/x/player/wbi/playurl": [playurl_ok()],
        },
        stream_routes={f"{STREAM_HOST}/a30216.m4s": AUDIO_BYTES},
    )
