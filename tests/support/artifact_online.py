"""Isolated online-extension fixtures and explicit policy configuration."""
from copy import deepcopy

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.services.artifact_policy import DEFAULT_POLICY, configure_policy
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from tests.test_artifact_transfer import archive as audio_archive


@pytest.fixture
def archive(tmp_path):
    fixture = audio_archive.__wrapped__(tmp_path)
    from bili_asr.storage.artifact_online import install_online_in_staged_copy
    with ArchiveSession(fixture.root, mode=ArchiveAccessMode.WRITE) as session:
        install_online_in_staged_copy(session.connection)
        catalog = ArtifactCatalog(session.connection)
        with session.connection:
            catalog.register_object(fixture.digest, len(fixture.data))
            catalog.bind_audio(1, fixture.digest)
    return fixture


def configure(archive, **changes):
    value = deepcopy(DEFAULT_POLICY)
    value.update(mode="offload", target_id="cold", minimum_age_seconds=0,
                 trigger_free_bytes=10**18, stop_free_bytes=10**18 + 1,
                 minimum_free_bytes=0, bytes_per_second=0)
    value.update(changes)
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        configure_policy(session.connection, "default", value)
    return value
