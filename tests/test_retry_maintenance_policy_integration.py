"""Maintenance cannot widen unified policy or erase spent physical budgets."""

import json

import pytest

from bili_asr.services.retry_maintenance import RetryPolicy, maintain_failed_jobs
from bili_asr.services.retry_maintenance_io import read_json
from bili_asr.services.workflow_retry import guarded_retry_failed
from bili_asr.services.workflow_status import (
    explain_workflow_job,
    workflow_status_report,
)
from tests.test_retry_maintenance import archive as archive_fixture
from tests.test_retry_maintenance import policy_value

archive = archive_fixture


def _state():
    return {'retry_holds': {}, 'retry_ledger': {}}


@pytest.mark.parametrize('code', ['RevisionValidationError', 'lease_expired', 'lease_renewal_failed',
                                  'worker_drain_timeout', 'DeepSeekError', 'deepseek_http_402'])
def test_deployment_allowlist_cannot_grant_an_automatic_exception(code):
    value = policy_value()
    value.update(kinds=['proofread'], retryable_errors={'proofread': [code]})
    with pytest.raises(ValueError, match='unsafe maintenance'):
        RetryPolicy.load(value)


def test_fresh_ledger_never_resets_database_retry_budget(archive, tmp_path):
    _root, connection, _repository, job = archive
    with connection:
        connection.execute('UPDATE workflow_jobs SET attempt_count=3 WHERE job_id=?', (job.job_id,))
    state = _state()
    result = maintain_failed_jobs(connection, RetryPolicy.load(policy_value()), state,
                                 state_path=tmp_path / 'state.json', now=10000)
    assert result[0]['reason'] == 'budget_exhausted'
    assert state['retry_ledger'] == {}
    assert connection.execute('SELECT count(*) FROM workflow_attempts').fetchone()[0] == 1


def test_active_repair_blocks_maintenance_without_charging(archive, tmp_path):
    _root, connection, _repository, job = archive
    with connection:
        connection.execute("""INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,
            available_at,created_at,updated_at) VALUES('repair','proofread','repair',?,'succeeded',0,0,0)""",
            (json.dumps({'repair_of_job_id': job.job_id}),))
    state = _state()
    result = maintain_failed_jobs(connection, RetryPolicy.load(policy_value()), state,
                                 state_path=tmp_path / 'state.json', now=10000)
    assert result[0]['reason'] == 'replacement_exists'
    assert not state['retry_ledger']


def test_repair_appearing_after_charge_is_refused_under_writer_fence(archive, tmp_path, monkeypatch):
    import bili_asr.services.retry_maintenance as service

    _root, connection, repository, job = archive
    original_write = service.write_json
    writes = 0

    def repair_during_charge(path, state):
        nonlocal writes
        original_write(path, state)
        writes += 1
        if writes == 1:
            with connection:
                connection.execute("""INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,
                    available_at,created_at,updated_at) VALUES('repair','proofread','repair',?,'queued',0,0,0)""",
                    (json.dumps({'repair_of_job_id': job.job_id}),))

    monkeypatch.setattr(service, 'write_json', repair_during_charge)
    state = _state()
    result = maintain_failed_jobs(connection, RetryPolicy.load(policy_value()), state,
                                 state_path=tmp_path / 'state.json', now=10000)
    assert result[0]['action'] == 'stale'
    assert state['retry_ledger'][job.job_id]['retries'] == 1
    assert repository.explain_job(job.job_id)['status'] == 'failed'


def test_control_change_after_charge_preserves_new_hold_and_reservation(archive, tmp_path, monkeypatch):
    import bili_asr.services.retry_maintenance as service

    _root, connection, repository, job = archive
    original_write = service.write_json
    path = tmp_path / 'state.json'

    def hold_during_charge(target, state):
        original_write(target, state)
        changed = read_json(target)
        changed['retry_holds'][job.job_id] = {'reason': 'new investigation'}
        original_write(target, changed)

    monkeypatch.setattr(service, 'write_json', hold_during_charge)
    with pytest.raises(ValueError, match='control state changed'):
        maintain_failed_jobs(connection, RetryPolicy.load(policy_value()), _state(), state_path=path, now=10000)
    stored = read_json(path)
    assert job.job_id in stored['retry_holds']
    assert stored['retry_ledger'][job.job_id]['dispatch_outcome'] == 'reserved'
    assert repository.explain_job(job.job_id)['status'] == 'failed'


def test_frozen_physical_budget_is_identical_in_status_cli_and_maintenance(tmp_path):
    from bili_asr.editorial import EditorialConfig
    from tests.test_ai_editorial import database
    from tests.test_editorial_regeneration import queued

    class DeepSeekTransportError(RuntimeError):
        error_code = 'deepseek_transport_failed'
        def __init__(self):
            self.safe_details = {'stage': 'provider_request', 'reason_code': 'transport_failed'}

    class Client:
        def complete(self, request, config):
            raise DeepSeekTransportError()

    generator = database.__wrapped__(tmp_path)
    connection = next(generator)
    try:
        workflow, editorial, _handlers, executor, _prepared, job_id = queued(
            connection, tmp_path, Client(), EditorialConfig(max_chunk_regenerations=0))
        assert executor.run().failed == 1
        with connection:
            connection.execute('UPDATE workflow_jobs SET last_error_code=?,available_at=1000 WHERE job_id=?',
                ('deepseek_transport_failed', job_id))
            connection.execute('UPDATE workflow_attempts SET error_code=?,finished_at=1000 WHERE job_id=?',
                ('deepseek_transport_failed', job_id))
        assert editorial.recovery_status_for_job(job_id)['exhausted']
        report = workflow_status_report(connection, no_control_state=True, include_jobs=True, now=10000)
        assert report['active_retryable_failures'] == 0
        explanation = explain_workflow_job(connection, job_id, no_control_state=True, now=10000)
        assert explanation['retry_decision']['reason'] == 'physical_budget_exhausted'
        retry = guarded_retry_failed(workflow, job_ids=[job_id], no_control_state=True,
                                     automatic=True, dry_run=True, now=10000)
        assert retry['decisions'][0]['reason'] == 'physical_budget_exhausted'
        value = policy_value()
        value.update(job_ids=[job_id], part_ids=[], kinds=['proofread'],
                     retryable_errors={'proofread': ['deepseek_transport_failed']})
        state = _state()
        maintenance = maintain_failed_jobs(connection, RetryPolicy.load(value), state,
                                          state_path=tmp_path / 'state.json', now=10000)
        assert maintenance[0]['reason'] == 'physical_budget_exhausted'
        assert not state['retry_ledger']
        assert connection.execute('SELECT count(*) FROM editorial_model_calls').fetchone()[0] == 1
    finally:
        generator.close()
