"""Every product consumer shares the complete-bundle declaration."""

from bili_asr import archive, artifacts
from bili_asr.cli import _shared
from bili_asr.services import transcript_projection


def test_bundle_consumers_share_the_required_artifact_key_object():
    assert archive._REQUIRED_ARTIFACT_KEYS is artifacts.REQUIRED_ARTIFACT_KEYS
    assert _shared._PRODUCT_PATH_KEYS is artifacts.REQUIRED_ARTIFACT_KEYS
    assert transcript_projection._PRODUCT_PATH_KEYS is artifacts.REQUIRED_ARTIFACT_KEYS
