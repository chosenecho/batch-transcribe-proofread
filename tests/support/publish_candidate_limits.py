from __future__ import annotations
import hashlib
from pathlib import Path
import pytest
from bili_asr.cli.main import main
from bili_asr.manifest import ManifestStore
from bili_asr.services.transcript_projection import ordered_candidates
from bili_asr.storage import TranscriptRepository, open_database
from tests.support.transcript_repository import (
    CHANGED_BODY,
    _record,
    _run,
    _video_with_parts,
)


def _seed_versions(connection):
    repository = TranscriptRepository(connection)
    # Insert later parts first. The first page of the earlier video holds no
    # transcript, and the captioned pages are gone upstream but still publishable.
    later = _video_with_parts(connection, "BV2LIMIT", (8101, 8102))
    earlier = _video_with_parts(
        connection, "BV1LIMIT", (8201, 8202, 8203), processing_status="gone"
    )
    writes = (
        (later[1], {}),
        (later[0], {}),
        (earlier[2], {}),
        (earlier[1], {}),
        (earlier[1], {"body": CHANGED_BODY}),
        (earlier[1], {"source_kind": "subtitle-ai", "language": "ai-en"}),
        (earlier[1], {"source_kind": "subtitle-ai", "language": "ai-zh"}),
        (earlier[1], {
            "source_kind": "subtitle-ai", "language": "ai-zh", "body": CHANGED_BODY,
        }),
    )
    for index, (part, kwargs) in enumerate(writes, 1):
        _record(repository, part, run_id=_run(repository, index), **kwargs)
    return repository, earlier, later
