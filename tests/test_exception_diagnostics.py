"""Unknown failures retain actionable codes without arbitrary exception text."""

import errno
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bili_asr.exception_diagnostics import capture_failure, filter_failure_details
from bili_asr.storage import WorkflowRepository, open_database
from bili_asr.workflow import WorkflowExecutor
from bili_asr.workflow_models import AsrPolicy, JobKind
from tests.test_workflow_control_plane import _profile, _seed_part


class ConnectError(RuntimeError):
    pass


def _domain_failures():
    from bili_asr.deepseek import DeepSeekError
    from bili_asr.editorial import RevisionValidationError
    from bili_asr.editorial_budget import BudgetExceeded
    from bili_asr.runtime_bindings import ModelBindingError
    return [(ModelBindingError("sensitive model path"), "model_identity_unverified"),
            (BudgetExceeded(3, ["input_token_limit"]), "editorial_budget_exceeded"),
            (RevisionValidationError("sensitive transcript"), "RevisionValidationError"),
            (DeepSeekError("sensitive provider body", error_code="deepseek_http_402"), "deepseek_http_402"),
            (ConnectError("sensitive transport URL?token=secret"), "ConnectError"),
            (type("ExternalError", (RuntimeError,), {"error_code": "sensitive-secret"})("secret"), "Exception"),
            (type("SensitiveTransportClassName", (RuntimeError,), {"error_code": "ConnectError"})("sensitive transport"), "Exception")]


@pytest.mark.parametrize("name,category,retry_policy,automatic_retry", [
    (name, "transient", "automatic", True) for name in (
        "TimeoutException", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "NetworkError", "RemoteProtocolError", "DeepSeekTransportError")
] + [("RuntimeCacheError", "configuration", "blocked", False)])
def test_registered_class_codes_keep_their_existing_policy(name, category, retry_policy, automatic_retry):
    from bili_asr.workflow_failure_diagnostics import execution_failure
    from bili_asr.workflow_failure_policy import failure_policy

    error = type(name, (RuntimeError,), {"error_code": "sensitive-forged-code"})(
        "sensitive endpoint?token=secret")
    details = {"phase": "workflow_execution", "exception_type": name}
    assert capture_failure(error) == details
    code, diagnostic = execution_failure(error)
    assert (code, diagnostic) == (name, details)
    policy = failure_policy(code)
    assert (policy.category, policy.retry_policy, policy.automatic_retry) == (
        category, retry_policy, automatic_retry)
    assert "sensitive" not in json.dumps((code, diagnostic))


def test_actual_domain_adapters_keep_their_error_codes():
    from bili_asr.asr.runtime_cache import RuntimeCacheError
    from bili_asr.deepseek import DeepSeekTransportError
    from bili_asr.workflow_failure_diagnostics import execution_failure

    assert execution_failure(DeepSeekTransportError("sensitive transport", error_code="deepseek_transport_failed"))[0] == (
        "deepseek_transport_failed")
    assert execution_failure(RuntimeCacheError("sensitive cache path")) == ("RuntimeCacheError", {
        "phase": "workflow_execution", "exception_type": "RuntimeCacheError"})


def test_legacy_connect_error_stays_named_without_granting_arbitrary_retry_codes():
    from bili_asr.workflow_failure_diagnostics import execution_failure

    secret = "sensitive private endpoint?token=secret"
    details = {"phase": "workflow_execution", "exception_type": "ConnectError"}
    assert capture_failure(ConnectError(secret)) == details
    assert execution_failure(ConnectError(secret)) == ("ConnectError", details)
    assert filter_failure_details({**details, "message": secret, "error_code": secret}) == details
    unknown = type("SensitiveTransportClassName", (RuntimeError,), {"error_code": "ConnectError"})(secret)
    assert execution_failure(unknown) == ("Exception", {
        "phase": "workflow_execution", "exception_type": "Exception"})
    assert "sensitive" not in json.dumps(execution_failure(unknown))


def test_os_error_and_invalid_child_fields_are_allowlisted():
    secret = "SESSDATA-sensitive/private/model?token=secret"
    diagnostic = capture_failure(PermissionError(errno.EACCES, secret), phase="decode", segment_ordinal=7)
    assert diagnostic == {"phase": "decode", "exception_type": "PermissionError", "errno": errno.EACCES,
                          "errno_name": "EACCES", "segment_ordinal": 7}
    assert filter_failure_details({**diagnostic, "message": secret, "traceback": secret,
        "sqlite_errorname": secret, "exception_type": secret, "phase": [], "reason": {},
        "chunk_index": 2**100}) == {"errno": errno.EACCES, "errno_name": "EACCES", "segment_ordinal": 7}
    unknown = type("SecretTokenClassName", (RuntimeError,), {})(secret)
    assert capture_failure(unknown)["exception_type"] == "Exception"


def test_real_sqlite_cantopen_and_locked_errors_remain_distinguishable(tmp_path):
    try:
        sqlite3.connect(tmp_path / "sensitive-missing" / "archive.db")
    except sqlite3.OperationalError as exc:
        missing = capture_failure(exc)
    first = sqlite3.connect(tmp_path / "archive.db", timeout=0)
    second = sqlite3.connect(tmp_path / "archive.db", timeout=0)
    try:
        first.execute("CREATE TABLE test(value)")
        first.execute("INSERT INTO test VALUES (1)")
        try:
            second.execute("INSERT INTO test VALUES (2)")
        except sqlite3.OperationalError as exc:
            busy = capture_failure(exc, phase="result_commit")
    finally:
        first.close()
        second.close()
    assert missing["sqlite_errorname"] == "SQLITE_CANTOPEN" and missing["sqlite_errorcode"] == 14
    assert busy["sqlite_errorname"] == "SQLITE_BUSY" and busy["sqlite_errorcode"] == 5
    assert "sensitive" not in json.dumps([missing, busy])


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_NOFILE fault injection is POSIX-only")
def test_sqlite_fd_exhaustion_is_captured_in_an_isolated_process(tmp_path):
    script = """
import json,os,resource,sqlite3,sys
from bili_asr.exception_diagnostics import capture_failure
resource.setrlimit(resource.RLIMIT_NOFILE, (64, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
handles=[]
try:
 while True: handles.append(open(os.devnull))
except OSError as exc: fd=capture_failure(exc)
try: sqlite3.connect(sys.argv[1])
except sqlite3.Error as exc: db=capture_failure(exc)
for handle in handles: handle.close()
print(json.dumps({'fd':fd,'db':db}))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "archive.db")], capture_output=True, text=True, check=True)
    details = json.loads(result.stdout)
    assert details["fd"]["errno"] == errno.EMFILE
    assert details["db"]["sqlite_errorcode"] == sqlite3.SQLITE_CANTOPEN


@pytest.mark.parametrize("error,expected", [(OSError(errno.EMFILE, "sensitive .env SESSDATA secret"), "OSError"), *_domain_failures()])
def test_generic_executor_persists_safe_diagnostic_and_attempt_identity(tmp_path, error, expected):
    connection = open_database(tmp_path)
    try:
        _seed_part(connection)
        repository = WorkflowRepository(connection)
        repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=_profile(repository))
        def fail(_job):
            raise error
        worker = WorkflowExecutor(repository, worker_id="test", handlers={JobKind.SUBTITLE: fail}, kinds=(JobKind.SUBTITLE,))
        assert worker.run().failed == 1
        row = connection.execute("SELECT job_id,error_code,result_json FROM workflow_attempts").fetchone()
        diagnostic = json.loads(row["result_json"])["diagnostic"]
        assert row["error_code"] == expected
        assert diagnostic["job_id"] == row["job_id"] and diagnostic["attempt_count"] == 1
        if isinstance(error, OSError):
            assert diagnostic["errno"] == errno.EMFILE and diagnostic["phase"] == "workflow_execution"
        assert "sensitive" not in row["result_json"]
    finally:
        connection.close()


def test_persistence_failure_emits_independent_minimum_signal_without_exception_text(capsys):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("sensitive private path cookie")
    worker = WorkflowExecutor(SimpleNamespace(fail=fail), worker_id="test", handlers={})
    with pytest.raises(sqlite3.OperationalError):
        worker._fail(SimpleNamespace(job_id="trusted-job", attempt_count=3), "OSError")
    signal = json.loads(capsys.readouterr().err)
    assert signal["event"] == "workflow_failure_persist_failed"
    assert signal["diagnostic"]["phase"] == "failure_persistence"
    assert "sensitive" not in json.dumps(signal)


def test_failed_independent_signal_does_not_replace_the_persistence_error(monkeypatch):
    original = sqlite3.OperationalError("sensitive original persistence failure")
    def fail(*args, **kwargs):
        raise original
    def broken_stderr(_value):
        raise BrokenPipeError("stderr is gone")
    monkeypatch.setattr("bili_asr.diagnostics.write_stderr", broken_stderr)
    worker = WorkflowExecutor(SimpleNamespace(fail=fail), worker_id="test", handlers={})
    with pytest.raises(sqlite3.OperationalError) as caught:
        worker._fail(SimpleNamespace(job_id="trusted-job", attempt_count=3), "OSError")
    assert caught.value is original
