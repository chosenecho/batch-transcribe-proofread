'''Upstream features compose with the collector-free local input boundary.'''
import json
from pathlib import Path
import sqlite3
import time

import pytest

from bili_asr.cli.main import main
from bili_asr.cli.local_input import import_manifest
from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS
from bili_asr.services.transcription_targets import load_youtube_targets
from bili_asr.workflow_supervisor import WorkerSupervisor
from tests.test_local_processing import inputs as inputs


def test_processing_commands_keep_collection_unavailable(tmp_path):
    parser = build_parser()
    assert {'input', 'transcribe', 'production', 'delivery'} <= COMMANDS.keys()
    assert not {'source', 'fetch-meta', 'fetch-tags', 'user-content'} & COMMANDS.keys()
    for removed in ('source', 'fetch-meta', 'fetch-tags', 'user-content'):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])
    assert parser.parse_args(['workflow', 'run', '--role', 'cpu']).role == 'cpu'
    with pytest.raises(SystemExit):
        parser.parse_args(['workflow', 'run', '--role', 'acquisition'])
    with pytest.raises(ValueError, match='unsupported worker role'):
        WorkerSupervisor(archive_root=tmp_path, slots={'acquisition': 1})


def test_all_imported_parts_compose_with_persistent_batches(inputs, capsys):
    root, _, manifest, _ = inputs
    imported = import_manifest(manifest, root)
    assert main(['workflow', 'plan', '--archive-root', str(root), '--all-parts',
                 '--model', 'offline', '--device', 'cpu', '--proofread']) == 0
    capsys.readouterr()
    with sqlite3.connect(root / 'archive.db') as db:
        assert db.execute('SELECT DISTINCT kind FROM workflow_jobs ORDER BY kind').fetchall() == [
            ('asr',), ('proofread',), ('render_document',)]
        job_ids = [row[0] for row in db.execute('SELECT job_id FROM workflow_jobs ORDER BY job_id')]
        assert len(job_ids) == 3 * len(imported['part_ids'])
    assert main(['archive', 'install-workflow-batches', '--archive-root', str(root)]) == 0
    capsys.readouterr()
    prefix = ['workflow', 'batch', '--archive-root', str(root)]
    arguments = [*prefix, 'create', '--workers', '1', '--actor', 'test', '--reason', 'local input']
    for job in job_ids:
        arguments += ['--job-id', job]
    assert main(arguments) == 0
    batch = json.loads(capsys.readouterr().out)
    for action, state in [('pause', 'paused'), ('resume', 'active')]:
        assert main([*prefix, action, '--batch-id', batch['batch_id'], '--actor', 'test',
                     '--reason', 'reviewed local batch']) == 0
        assert json.loads(capsys.readouterr().out)['state'] == state
    with sqlite3.connect(root / 'archive.db') as db:
        assert db.execute('SELECT count(*) FROM workflow_attempts').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM workflow_batch_members').fetchone()[0] == len(job_ids)


def test_offline_cloud_selection_deduplicates_without_network(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request',
                        lambda *a, **kw: pytest.fail('selection must stay offline'))
    targets = tmp_path / 'targets.json'
    targets.write_text(json.dumps([
        {'youtube_url': 'https://www.youtube.com/watch?v=ABCDEFGHIJK', 'bvid': 'BV123'},
        {'youtube_id': 'ABCDEFGHIJK'}, {'youtube_url': ''},
    ]), encoding='utf-8')
    selected = load_youtube_targets(input_path=targets)
    assert len(selected) == 1
    assert selected[0].as_dict()['external_video_id'] == 'ABCDEFGHIJK'
    assert selected[0].reference_bvids == ('BV123',)


def _cooperative_controller(options, role, worker_id):
    root = Path(options['archive_root'])
    (root / 'ready').write_text(worker_id, encoding='utf-8')
    deadline = time.monotonic() + 15
    while not Path(options['drain_file']).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('drain was never delivered')
        time.sleep(.01)
    time.sleep(.15)
    (root / 'finished').write_text(role, encoding='utf-8')


def test_supervisor_drains_owned_child_before_forced_termination(tmp_path):
    drain = tmp_path / 'drain'
    supervisor = WorkerSupervisor(archive_root=tmp_path, slots={'cpu': 1},
        controller=_cooperative_controller, drain_file=str(drain), drain_timeout=.5)
    try:
        supervisor.tick()
        deadline = time.monotonic() + 15
        while not (tmp_path / 'ready').exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        drain.touch()
        supervisor.close()
        assert (tmp_path / 'finished').read_text('utf-8') == 'cpu'
        assert all(slot.process is None for slot in supervisor.slots)
    finally:
        supervisor.close()
