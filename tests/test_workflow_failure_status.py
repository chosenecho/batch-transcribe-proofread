"""Failures are jobs, attempts, controls, and recovery relationships, not one total."""

import json
import sqlite3

import pytest

from bili_asr.archive_session import ArchiveAccessMode
from bili_asr.cli import main
from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS
from bili_asr.services.workflow_status import (
    explain_workflow_job,
    workflow_status_report,
)
from bili_asr.storage import open_database
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    _seed_part(connection)
    yield connection
    connection.close()


def _job(db, job_id='failed', *, status='failed', error='deepseek_transport_failed', attempts=1,
         kind='proofread', payload=None):
    with db:
        db.execute("""INSERT INTO workflow_jobs(job_id,kind,video_part_id,dedupe_key,payload_json,
            status,available_at,attempt_count,last_error_code,created_at,updated_at)
            VALUES (?,?,1,?,?,?,?,?,?,1,20)""", (job_id, kind, job_id, json.dumps(payload or {}),
                status, 0, attempts, error if status == 'failed' else None))


def _attempt(db, job='failed', *, attempt='attempt', start=5, finish=20, outcome='failed',
             error='deepseek_transport_failed', result=None):
    with db:
        db.execute("""INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,
            outcome,error_code,result_json) VALUES (?,?,'test',?,?,?,?,?)""",
            (attempt, job, start, finish, outcome, error, None if result is None else json.dumps(result)))


def _controls(tmp_path, holds=None, ledger=None):
    path = tmp_path / 'control.json'
    path.write_text(json.dumps({'retry_holds': holds or {}, 'retry_ledger': ledger or {}}), encoding='utf-8')
    return path


def test_successful_retry_preserves_history_and_finish_window(db):
    _job(db, 'success', status='succeeded', attempts=2)
    _attempt(db, 'success', attempt='old', start=5, finish=15)
    _attempt(db, 'success', attempt='new', start=16, finish=21, outcome='succeeded', error=None)
    _job(db)
    _attempt(db, start=1, finish=20)
    before = db.total_changes
    report = workflow_status_report(db, since=10, until=20, now=100, include_jobs=True)
    assert report['current_failed_jobs'] == 1
    assert report['historical_failed_attempts'] == 2
    assert report['failures_since'] == 1
    assert report['window']['time_field'] == 'finished_at'
    assert report['active_retryable_failures'] is None
    assert report['retry_holds'] is None
    assert report['groups'][0]['historical_failed_attempts'] == 2
    assert report['groups'][0]['policy']['policy_version']
    assert db.total_changes == before
    assert not db.in_transaction


def test_group_last_occurrence_uses_failure_time_not_later_job_mutation(db):
    _job(db)
    _attempt(db, finish=20)
    with db:
        db.execute("UPDATE workflow_jobs SET updated_at=1000 WHERE job_id='failed'")
    report = workflow_status_report(db, now=2000)
    assert report['groups'][0]['last_occurrence'] == 20
    assert report['schema_identity'] == 'workflow-failure-status/v1'


def test_known_controls_holds_budget_and_replacements(db, tmp_path):
    for identity in ('eligible', 'held', 'replaced', 'exhausted'):
        _job(db, identity, attempts=3 if identity == 'exhausted' else 1)
        _attempt(db, identity, attempt=identity)
    _job(db, 'repair', status='succeeded', payload={'repair_of_job_id': 'replaced'})
    path = _controls(tmp_path, holds={'held': {'reason': 'private reason'}})
    report = workflow_status_report(db, now=10000, failed_only=True, control_state_path=path)
    assert report['current_failed_jobs'] == 4
    assert report['retry_holds'] == 1
    assert report['active_retryable_failures'] == 1
    assert report['failed_jobs_with_active_replacements'] == 1
    jobs = {job['job_id']: job for job in report['jobs']}
    assert jobs['held']['retry_decision']['reason'] == 'retry_hold'
    assert jobs['exhausted']['retry_decision']['reason'] == 'budget_exhausted'
    assert jobs['replaced']['retry_decision']['reason'] == 'replacement_exists'
    assert jobs['replaced']['recovery']['next_action'] == 'follow_replacement_job'
    assert jobs['replaced']['replacements'] == [{'job_id': 'repair', 'kind': 'proofread', 'status': 'succeeded'}]
    assert 'private reason' not in json.dumps(report)
    declared = workflow_status_report(db, now=10000, no_control_state=True)
    assert declared['retry_holds'] == 0
    assert declared['active_retryable_failures'] == 2


@pytest.mark.parametrize('result,state', [
    ({'diagnostic': {'stage': 'revision_validation', 'reason_code': 'invalid_reference', 'paragraph_index': 3}}, 'available'),
    ({'diagnostic': {'raw_body': 'secret'}}, 'redacted'),
    ({'diagnostic': {'stage': 'secret cookie with spaces'}}, 'redacted'),
    ({'raw_response': 'secret'}, 'missing'),
])
def test_safe_diagnostic_projection_and_repair_guidance(db, result, state):
    _job(db, error='RevisionValidationError')
    _attempt(db, error='RevisionValidationError', result=result)
    report = explain_workflow_job(db, 'failed', no_control_state=True, now=10000)
    assert report['diagnostic_state'] == state
    assert 'secret' not in json.dumps(report)
    assert 'repair-proofread' in report['recovery']['command']
    assert '--control-state' in report['recovery']['alternative_command']
    assert report['policy']['automatic_retry'] is False


def test_source_recovery_requires_new_transcript(db):
    _job(db, error='editorial_source_repetition')
    _attempt(db, error='editorial_source_repetition')
    report = explain_workflow_job(db, 'failed', now=10000)
    assert '--base-transcript-id' in report['recovery']['command']
    assert report['recovery']['alternative_command'] is None


def test_paginated_details_never_hide_a_blocker_or_active_replacement(db):
    _job(db, 'waiting', status='queued')
    _job(db, 'original')
    with db:
        for index in range(101):
            identity = f'dep-{index:03d}'
            _job(db, identity, status='failed' if index == 100 else 'succeeded')
            db.execute('INSERT INTO workflow_job_dependencies VALUES (?,?)', ('waiting', identity))
            _job(db, f'repair-{index:03d}', status='succeeded' if index == 100 else 'failed',
                 payload={'repair_of_job_id': 'original'})
    waiting = explain_workflow_job(db, 'waiting', no_control_state=True, now=10000)
    assert waiting['ready'] is False and waiting['blocked'] is True
    assert waiting['blocker_count'] == 1 and waiting['prerequisite_count'] == 101
    assert waiting['prerequisites_truncated'] is True
    original = explain_workflow_job(db, 'original', no_control_state=True, now=10000)
    assert original['retry_decision']['reason'] == 'replacement_exists'
    assert original['replacement_count'] == 101
    assert original['replacements_truncated'] is True


def test_transitive_cancellation_counts_and_job_identity_are_preserved(db):
    _job(db, 'audio', status='cancelled', kind='audio')
    _job(db, 'asr', status='queued')
    _job(db, 'proofread', status='queued')
    with db:
        db.execute('INSERT INTO workflow_job_dependencies VALUES (?,?)', ('asr', 'audio'))
        db.execute('INSERT INTO workflow_job_dependencies VALUES (?,?)', ('proofread', 'asr'))
    report = workflow_status_report(db, include_jobs=True)
    assert report['blocked_by_cancelled'] == 2
    jobs = {job['job_id']: job for job in report['jobs']}
    assert jobs['asr']['blocked_by_cancelled'] is True
    assert jobs['proofread']['blocked_by_cancelled'] is True
    assert jobs['proofread']['cancelled_blocker_count'] == 0
    assert jobs['proofread']['blocked'] is True and jobs['proofread']['ready'] is False


def test_cli_json_text_parity_and_readonly_retry(db, tmp_path, capsys):
    _job(db)
    _attempt(db, result={'diagnostic': {'stage': 'provider_request', 'reason_code': 'transport_failed'}})
    controls = _controls(tmp_path)
    args = ['workflow', 'status', '--archive-root', str(tmp_path), '--failed', '--since', '10', '--until', '30',
            '--control-state', str(controls)]
    assert main(args + ['--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    text = capsys.readouterr().out
    for field in ('current_failed_jobs', 'historical_failed_attempts', 'failures_since', 'retry_holds'):
        assert f'{field}: {report[field]}' in text
    parsed = build_parser().parse_args(['workflow', 'retry', '--dry-run'])
    assert COMMANDS['workflow'].database_for(parsed) is ArchiveAccessMode.READ
    dbpath = tmp_path / 'archive.db'
    before = dbpath.read_bytes()
    retry_args = ['workflow', 'retry', '--archive-root', str(tmp_path), '--job-id', 'failed',
                  '--no-control-state', '--dry-run', '--json']
    assert main(retry_args) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision['dry_run'] is True and decision['requeued'] == 0
    assert decision['decisions'][0]['allowed'] is True
    assert dbpath.read_bytes() == before
    assert db.execute("SELECT status FROM workflow_jobs WHERE job_id='failed'").fetchone()[0] == 'failed'


def test_missing_controls_retry_reports_denial_without_changing_evidence(db, tmp_path, capsys):
    _job(db)
    _attempt(db)
    assert main(['workflow', 'retry', '--archive-root', str(tmp_path), '--job-id', 'failed', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['requeued'] == 0
    assert result['decisions'][0]['reason'] == 'control_state_unknown'
    assert db.execute('SELECT count(*) FROM workflow_attempts').fetchone()[0] == 1


def test_cli_retry_to_success_preserves_failed_attempt_and_safe_diagnostic(db, tmp_path, capsys):
    from bili_asr.storage.workflow import WorkflowRepository
    from bili_asr.workflow_models import JobKind

    _job(db, kind='audio', payload={'video_part_id': 1})
    _attempt(db, result={'diagnostic': {'stage': 'provider_request', 'reason_code': 'transport_failed'}})
    args = ['workflow', 'status', '--archive-root', str(tmp_path), '--json', '--failed', '--no-control-state']
    assert main(args) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial['jobs'][0]['diagnostic']['reason_code'] == 'transport_failed'
    assert main(['workflow', 'retry', '--archive-root', str(tmp_path), '--job-id', 'failed',
                 '--no-control-state', '--manual', '--json']) == 0
    retry = json.loads(capsys.readouterr().out)
    assert retry['requeued'] == 1
    workflow = WorkflowRepository(db)
    job = workflow.claim('recovered', kinds=[JobKind.AUDIO])
    assert job.job_id == 'failed'
    workflow.finish(job.job_id, worker_id='recovered', result={'audio_id': 1})
    assert main(args) == 0
    final = json.loads(capsys.readouterr().out)
    assert final['current_failed_jobs'] == 0
    assert final['historical_failed_attempts'] == 1
    assert final['jobs'] == []
    failed = db.execute("SELECT error_code,result_json FROM workflow_attempts WHERE outcome='failed'").fetchone()
    assert failed['error_code'] == 'deepseek_transport_failed'
    assert json.loads(failed['result_json'])['diagnostic']['reason_code'] == 'transport_failed'


@pytest.mark.parametrize('kwargs', [{'since': 30, 'until': 20}, {'limit': 0}, {'offset': -1}])
def test_report_validates_bounds(db, kwargs):
    with pytest.raises(ValueError):
        workflow_status_report(db, **kwargs)


def test_diagnostic_envelope_byte_and_json_validity_guard(db):
    _job(db)
    _attempt(db)
    with db:
        db.execute("UPDATE workflow_attempts SET result_json='invalid-json'")
    assert explain_workflow_job(db, 'failed')['diagnostic_state'] == 'unavailable'
    with db:
        db.execute('UPDATE workflow_attempts SET result_json=?',
            (json.dumps({'diagnostic': {'stage': 'provider_request'}, 'output': '秘密' * 9000}, ensure_ascii=False),))
    assert explain_workflow_job(db, 'failed')['diagnostic_state'] == 'unavailable'


def test_readonly_report_accepts_sqlite_query_only_connection(db):
    _job(db)
    db.execute('PRAGMA query_only=ON')
    assert workflow_status_report(db, no_control_state=True)['current_failed_jobs'] == 1
    with pytest.raises(sqlite3.OperationalError):
        db.execute("UPDATE workflow_jobs SET status='queued'")
