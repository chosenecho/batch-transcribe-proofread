"""Construct frozen pre-rights releases without changing their legacy schema."""
from unittest.mock import patch

from bili_asr.publication import publish_edition


def publish_legacy_edition(*args, **kwargs):
    # Test-only historical builder: old releases existed before rights checks.
    # These releases must now be denied by normal public export until granted.
    with (patch('bili_asr.content_rights.require_public_rights', return_value=None),
          patch('bili_asr.content_rights.require_publication_enabled', return_value=None)):
        return publish_edition(*args, **kwargs)
