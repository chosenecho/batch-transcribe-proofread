"""A single safe projection for human and JSON workflow diagnostics."""

from __future__ import annotations

import json
import shlex
import sqlite3
import time
from typing import Any

from bili_asr.services.workflow_retry import decide_retry, load_control_state
from bili_asr.storage import workflow_status as facts
from bili_asr.storage.workflow_retry import editorial_retry_facts
from bili_asr.workflow_errors import safe_job_details
from bili_asr.workflow_failure_policy import failure_policy


def _policy(code: str | None) -> dict[str, Any]:
    return failure_policy(code).as_dict()


def _control_summary(control) -> dict[str, Any]:
    return control.as_dict()


def _project(row: dict, *, control, now: int, replacements: list[dict], prerequisites: list[dict],
             editorial_retry: dict | None = None) -> dict:
    row = dict(row)
    encoded = row.pop('diagnostic_json')
    state = row.pop('diagnostic_state')
    diagnostic = None
    if encoded is not None:
        try:
            diagnostic = safe_job_details(json.loads(encoded))
            state = 'available'
        except (ValueError, TypeError, RecursionError):
            state = 'redacted'
    attempt = None
    if row['outcome'] is not None:
        attempt = {key: row[key] for key in ('worker_id', 'outcome', 'started_at', 'finished_at')}
        attempt['error_code'] = row['attempt_error_code']
    for key in ('worker_id', 'outcome', 'started_at', 'finished_at', 'attempt_error_code'):
        row.pop(key)
    blockers = [item for item in prerequisites if item['status'] != 'succeeded']
    queued = row['status'] == 'queued'
    row.update(last_attempt=attempt, diagnostic=diagnostic, diagnostic_state=state,
               prerequisites=prerequisites, blockers=blockers, blocked=queued and row['blocker_count'] > 0,
               ready=queued and row['blocker_count'] == 0 and row['available_at'] <= now,
               blocked_by_cancelled=queued and bool(row.pop('has_cancelled_ancestor')),
               replacements=replacements, replacement_limit=100, dependency_limit=100,
               replacements_truncated=row['replacement_count'] > len(replacements),
               prerequisites_truncated=row['prerequisite_count'] > len(prerequisites))
    row['policy'] = _policy(row['error_code'])
    derived_code = None if editorial_retry is None else editorial_retry.get('reason')
    if derived_code in {'deepseek_content_filtered', 'deepseek_unsupported_completion', 'deepseek_output_interrupted'}:
        # Keep historical codes and diagnostics intact. This finite, explicitly
        # labelled projection comes from replaying the preserved envelope.
        row['provider_termination'] = {'error_code': derived_code,
            'diagnostic_source': 'stored_response_replay', 'policy': _policy(derived_code)}
    decision_input = {**row, 'latest_finished_at': None if attempt is None else attempt['finished_at'],
                      'editorial_retry': editorial_retry}
    row['retry_decision'] = decide_retry(decision_input, control_state=control, now=now,
                                        automatic=True, replacements=replacements)
    row['recovery'] = _recovery(row)
    return row


def _recovery(row: dict) -> dict:
    code = row['error_code'] or 'unknown'
    command = None
    alternative = None
    action = row['policy']['recommended_action']
    job_arg = shlex.quote(row['job_id'])
    if row.get('provider_termination'):
        action = row['provider_termination']['policy']['recommended_action']
    elif row['status'] == 'failed' and row['kind'] == 'proofread' and (
            row['policy']['retry_policy'] == 'repair' or row['policy']['category'] == 'revision_validation'):
        command = f'bili-asr workflow repair-proofread --archive-root <archive-root> --job-id {job_arg}'
        if code == 'editorial_source_repetition':
            action = 'review_source_and_store_corrected_transcript_before_repair'
            command += ' --base-transcript-id <corrected-transcript-id>'
        else:
            action = 'inspect_revision_then_repair_with_changed_configuration_or_retry_frozen_input'
            command += ' --max-chunk-segments <changed-positive-limit>'
            alternative = (f'bili-asr workflow retry --archive-root <archive-root> --manual --job-id {job_arg}'
                           ' --control-state <state-file> --dry-run')
    elif row['status'] == 'failed' and row['policy']['retry_policy'] in {'automatic', 'manual'}:
        command = (f'bili-asr workflow retry --archive-root <archive-root> --manual --job-id {job_arg}'
                   ' --control-state <state-file> --dry-run')
    if any(item['status'] in {'queued', 'running', 'succeeded'} for item in row['replacements']):
        action, command, alternative = 'follow_replacement_job', None, None
    return {'next_action': action, 'command': command, 'alternative_command': alternative,
            'command_shell': 'posix', 'commands_are_templates': True,
            'control_guidance': 'provide_control_state_or_explicitly_declare_no_external_controls'}


def workflow_status_report(connection: sqlite3.Connection, *, since: int | None = None,
                           until: int | None = None, limit: int = 100, offset: int = 0,
                           failed_only: bool = False, include_jobs: bool = False,
                           control_state_path=None, no_control_state: bool = False,
                           now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    until = now + 1 if until is None else until
    if since is not None and (since < 0 or since >= until):
        raise ValueError('--since must be a non-negative epoch second before --until')
    if until < 0 or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError('--until and --offset must be non-negative; --limit must be 1-1000')
    control = load_control_state(control_state_path, no_control_state=no_control_state, connection=connection, now=now)
    with facts.read_snapshot(connection):
        report = facts.counts(connection, since=since, until=until)
        groups = facts.failure_groups(connection, since=since, until=until)
        by_key = {(item['kind'], item['error_code']): item for item in groups}
        for item in groups:
            item.update(policy=_policy(item['error_code']), retry_decisions={}, active_retryable_failures=0)
        allowed = 0
        superseded = 0
        editorial = editorial_retry_facts(connection, (dict(row) for row in connection.execute(
            "SELECT job_id,kind FROM workflow_jobs WHERE status='failed' AND kind='proofread'")))
        for raw in facts.failed_facts(connection):
            row = dict(raw)
            row['editorial_retry'] = editorial.get(row['job_id'])
            replaced = bool(row.pop('has_replacement'))
            replacements = [{'status': 'queued'}] if replaced else []
            decision = decide_retry(row, control_state=control, now=now, automatic=True,
                                    replacements=replacements)
            group = by_key[(row['kind'], row['error_code'])]
            reason = decision['reason']
            group['retry_decisions'][reason] = group['retry_decisions'].get(reason, 0) + 1
            group['active_retryable_failures'] += int(decision['allowed'])
            allowed += int(decision['allowed'])
            superseded += int(replaced)
        if control.status == 'not_provided':
            allowed = None
            for item in groups:
                item['active_retryable_failures'] = None
        report.update(schema_version=1, schema_identity='workflow-failure-status/v1',
            snapshot_at=now, window={'since': since, 'until': until,
            'time_field': 'finished_at', 'bounds': '[since,until)'}, groups=groups,
            active_retryable_failures=allowed, retry_holds=_control_summary(control)['retry_holds'],
            control_state=_control_summary(control), failed_jobs_with_active_replacements=superseded,
            jobs=[], pagination={'limit': limit, 'offset': offset, 'total': 0, 'returned': 0})
        if include_jobs or failed_only:
            total, rows = facts.job_page(connection, failed_only=failed_only, limit=limit, offset=offset)
            ids = tuple(row['job_id'] for row in rows)
            repairs = facts.replacements(connection, ids)
            prerequisites = facts.prerequisites(connection, ids)
            page_editorial = editorial_retry_facts(connection, rows)
            report['jobs'] = [_project(row, control=control, now=now,
                replacements=repairs.get(row['job_id'], []), prerequisites=prerequisites.get(row['job_id'], []),
                editorial_retry=page_editorial.get(row['job_id']))
                for row in rows]
            report['pagination'].update(total=total, returned=len(rows))
    return report


def explain_workflow_job(connection: sqlite3.Connection, job_id: str, *, control_state_path=None,
                         no_control_state: bool = False, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    control = load_control_state(control_state_path, no_control_state=no_control_state, connection=connection, now=now)
    with facts.read_snapshot(connection):
        row = facts.job_fact(connection, job_id)
        repairs = facts.replacements(connection, (job_id,))
        prerequisites = facts.prerequisites(connection, (job_id,))
        editorial = editorial_retry_facts(connection, (row,))
        return _project(row, control=control, now=now, replacements=repairs.get(job_id, []),
                        prerequisites=prerequisites.get(job_id, []), editorial_retry=editorial.get(job_id))


def render_status_text(report: dict, *, jobs: bool = False, details: bool = False) -> str:
    lines = [f"{state}: {report['status_counts'].get(state, 0)}"
             for state in ('queued', 'running', 'succeeded', 'failed', 'cancelled')]
    lines.append(f"blocked_by_cancelled: {report['blocked_by_cancelled']}")
    for field in ('current_failed_jobs', 'historical_failed_attempts', 'failures_since',
                  'active_retryable_failures', 'retry_holds', 'failed_jobs_with_active_replacements'):
        value = report[field]
        lines.append(f"{field}: {'unknown' if value is None else value}")
    lines.append(f"control_state: {report['control_state']['status']}")
    lines.append(f"failure_window: finished_at [{report['window']['since']},{report['window']['until']})")
    for group in report['groups']:
        lines.append('  failure_group: ' + json.dumps(group, ensure_ascii=False, sort_keys=True))
    if jobs:
        for row in report['jobs']:
            lines.append(f"  job_id={row['job_id']} kind={row['kind']} status={row['status']} "
                f"bvid={row['bvid']} page_index={row['page_index']} video_part_id={row['video_part_id']} "
                f"attempts={row['attempt_count']} blocked_by_cancelled={int(row['blocked_by_cancelled'])}")
    if details:
        lines.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in report['jobs'])
    if jobs or details:
        lines.append('pagination: ' + json.dumps(report['pagination'], sort_keys=True))
    return '\n'.join(lines)
