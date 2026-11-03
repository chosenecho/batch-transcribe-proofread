"""The content-permission and public-output module requires explicit opt-in."""
import pytest

from bili_asr import cli
from bili_asr.content_rights import ContentRightsError, require_publication_enabled


@pytest.mark.parametrize('value', [None, '', '0', 'true', 'yes', ' 1'])
def test_only_exact_explicit_opt_in_activates_publication(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('BILI_PUBLICATION_ENABLED', raising=False)
    else:
        monkeypatch.setenv('BILI_PUBLICATION_ENABLED', value)
    with pytest.raises(ContentRightsError, match='disabled by default'):
        require_publication_enabled()
    monkeypatch.setenv('BILI_PUBLICATION_ENABLED', '1')
    require_publication_enabled()


@pytest.mark.parametrize('action', [
    ['rights', 'init'],
    ['rights', 'show', '--edition-id', 'missing'],
    ['rights', 'grant', '--edition-id', 'missing', '--permission-file', 'missing.json', '--actor', 'admin', '--note', 'test'],
    ['rights', 'revoke', '--edition-id', 'missing', '--actor', 'admin', '--note', 'test'],
    ['rights', 'hold', '--video-part-id', '1', '--actor', 'admin', '--note', 'test'],
    ['rights', 'clear', '--video-part-id', '1', '--actor', 'admin', '--note', 'test'],
    ['publish', '--edition-id', 'missing', '--actor', 'admin'],
    ['export', '--out', 'unused'],
    ['export-drafts', '--out', 'unused'],
])
def test_disabled_cli_refuses_before_opening_archive_or_resolving_artifacts(tmp_path, monkeypatch, capsys, action):
    monkeypatch.delenv('BILI_PUBLICATION_ENABLED', raising=False)
    root = tmp_path / 'absent-archive'
    assert cli.main(['publication', *action, '--archive-root', str(root)]) == 1
    assert 'BILI_PUBLICATION_ENABLED=1' in capsys.readouterr().err
    assert not root.exists()
