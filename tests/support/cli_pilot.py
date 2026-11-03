import bili_asr.asr.runner as _module_asr_runner
import bili_asr.cli.pilot as _module_cli_pilot
import json
import os
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
