"""Evaluation replays actual stored bytes and never relaxes the validator."""
import json
from copy import deepcopy

import pytest

from bili_asr.editorial import EditorialConfig
from bili_asr.services.editorial_evaluation import load_sample, replay_sample
from bili_asr.services.editorial_provider_evaluation import evaluate_provider
from bili_asr.services.editorial_provider_evaluation import evaluate_campaign
from tests import test_ai_editorial as fixtures
from tests.test_editorial_regeneration import ReferenceClient, queued


@pytest.fixture
def database(tmp_path):
    yield from fixtures.database.__wrapped__(tmp_path)


def saved_failure(database, tmp_path):
    _, _, _, executor, _, _ = queued(database, tmp_path, ReferenceClient(failures=10),
                                    EditorialConfig(max_chunk_regenerations=0))
    assert executor.run().failed == 1
    return database.execute("SELECT call_id FROM editorial_model_calls").fetchone()[0]


def test_replay_is_read_only_and_feedback_is_only_a_preview(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    before = (tmp_path / "archive.db").read_bytes()
    sample = load_sample(tmp_path / "archive.db", call_id)
    original = deepcopy(sample)
    report = replay_sample(sample)
    assert report["validation"]["diagnostic"]["reason"] == "cross_paragraph_reference"
    assert report["feedback_preview"]["provider_called"] is False
    assert report["physical_calls"] == 0
    assert report["semantic_quality"] == "not_reviewed"
    assert sample == original
    assert (tmp_path / "archive.db").read_bytes() == before
    assert "messages" not in json.dumps(report) and "paragraphs\": [" not in json.dumps(report)


def test_replay_rejects_changed_request_instead_of_fabricating_match(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    row = database.execute("SELECT request_json FROM editorial_model_calls").fetchone()
    request = json.loads(row[0])
    request["request"]["messages"][1]["content"] = "{}"
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(request),))
    with pytest.raises(ValueError, match="fingerprint|frozen chunk"):
        load_sample(tmp_path / "archive.db", call_id)


def test_replay_rejects_changed_frozen_identity(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    prepared = json.loads(database.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0])
    prepared["snapshot"]["config"]["top_p"] = 1
    with database:
        database.execute("UPDATE editorial_inputs SET prepared_json=?", (json.dumps(prepared),))
    with pytest.raises(ValueError, match="identity"):
        load_sample(tmp_path / "archive.db", call_id)


@pytest.mark.parametrize("kind", ["response_replay", "local_precheck", "unknown"])
def test_replay_refuses_nonphysical_evidence(database, tmp_path, kind):
    call_id = saved_failure(database, tmp_path)
    request = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
    request["invocation_kind"] = kind
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(request),))
    with pytest.raises(ValueError, match="original physical"):
        load_sample(tmp_path / "archive.db", call_id)


@pytest.mark.parametrize("fingerprint_present", [True, False])
def test_replay_refuses_changed_sampling_controls(database, tmp_path, fingerprint_present):
    call_id = saved_failure(database, tmp_path)
    request = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
    request["request"]["top_p"] = 1
    if not fingerprint_present:
        del request["request_fingerprint"]
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(request),))
    with pytest.raises(ValueError, match="fingerprint|controls"):
        load_sample(tmp_path / "archive.db", call_id)


def test_legacy_raw_request_marks_absent_fingerprint(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    stored = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(stored["request"]),))
    assert replay_sample(load_sample(tmp_path / "archive.db", call_id))[
        "request_fingerprint_evidence"] == "unavailable_legacy"


def test_provider_budget_survives_restart_and_refuses_changed_identity(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    sample = load_sample(tmp_path / "archive.db", call_id)
    ledger = tmp_path / "private-experiment.db"
    client = ReferenceClient(failures=10)
    args = {"endpoint": "https://api.deepseek.com", "model": "deepseek-flash",
            "allow_paid_calls": True, "client": client}
    report = evaluate_provider(sample, ledger, **args)
    assert len(client.requests) == report["physical_calls"] == 4
    assert all(r["max_tokens"] == 8192 for r in client.requests)
    assert client.requests[0]["temperature"] == .2
    assert client.requests[2]["reasoning_effort"] == "low" and "temperature" not in client.requests[2]
    assert all(not r["validation"]["accepted"] for r in report["calls"])
    assert evaluate_provider(sample, ledger, **args)["physical_calls"] == 4
    assert len(client.requests) == 4
    args["model"] = "different-model"
    with pytest.raises(ValueError, match="identity or budget changed"):
        evaluate_provider(sample, ledger, **args)


def test_unknown_send_is_consumed_before_http_and_never_reissued(database, tmp_path):
    import sqlite3
    call_id = saved_failure(database, tmp_path)
    sample = load_sample(tmp_path / "archive.db", call_id)
    ledger = tmp_path / "private-experiment.db"
    class CrashClient:
        def complete(self, request, config):
            with sqlite3.connect(ledger) as other:
                assert other.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
            raise SystemExit("simulated process crash")
    args = {"endpoint": "https://api.deepseek.com", "model": "deepseek-flash", "allow_paid_calls": True}
    with pytest.raises(SystemExit):
        evaluate_provider(sample, ledger, client=CrashClient(), **args)
    client = ReferenceClient(failures=0)
    report = evaluate_provider(sample, ledger, client=client, **args)
    assert report["physical_calls"] == 2 and len(client.requests) == 1
    assert report["calls"][0]["outcome"] == "unknown_send_consumed"


def test_provider_requires_explicit_opt_in_before_creating_ledger(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    ledger = tmp_path / "private-experiment.db"
    with pytest.raises(ValueError, match="allow-paid-calls"):
        evaluate_provider(load_sample(tmp_path / "archive.db", call_id), ledger,
            endpoint="https://api.deepseek.com", model="deepseek-flash", allow_paid_calls=False)
    assert not ledger.exists()


def test_provider_refuses_existing_archive_as_ledger_without_source_writes(database, tmp_path):
    call_id = saved_failure(database, tmp_path)
    source = tmp_path / "archive.db"
    sample = load_sample(source, call_id)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="dedicated provider experiment"):
        evaluate_provider(sample, source, endpoint="https://api.deepseek.com", model="deepseek-flash",
                          allow_paid_calls=True, client=ReferenceClient(failures=0))
    assert source.read_bytes() == before


def test_provider_errors_consume_reservations_without_leaking_error_text(database, tmp_path):
    from bili_asr.deepseek import DeepSeekError
    call_id = saved_failure(database, tmp_path)
    sample = load_sample(tmp_path / "archive.db", call_id)
    class FailedClient:
        requests = 0
        def complete(self, request, config):
            self.requests += 1
            raise DeepSeekError("private-provider-body-secret")
    client = FailedClient()
    args = {"endpoint": "https://api.deepseek.com", "model": "deepseek-flash",
            "allow_paid_calls": True, "client": client}
    ledger = tmp_path / "experiment.db"
    report = evaluate_provider(sample, ledger, **args)
    assert report["physical_calls"] == client.requests == 2
    assert "private-provider-body-secret" not in json.dumps(report)
    assert all(not call["http_response_accepted"] for call in report["calls"])
    evaluate_provider(sample, ledger, **args)
    assert client.requests == 2


@pytest.mark.parametrize("tamper", ["manifest", "gap", "over_budget", "unknown_variant", "variant_budget", "schema"])
def test_provider_refuses_corrupt_ledger_before_http(database, tmp_path, tamper):
    import sqlite3
    sample = load_sample(tmp_path / "archive.db", saved_failure(database, tmp_path))
    ledger = tmp_path / "experiment.db"
    client = ReferenceClient(failures=0)
    args = {"endpoint": "https://api.deepseek.com", "model": "deepseek-flash",
            "allow_paid_calls": True, "client": client}
    evaluate_provider(sample, ledger, **args)
    assert len(client.requests) == 2
    with sqlite3.connect(ledger) as connection:
        if tamper == "manifest":
            connection.execute("UPDATE manifest SET body='{}'")
        elif tamper == "gap":
            connection.execute("DELETE FROM calls WHERE ordinal=1")
        elif tamper in {"over_budget", "variant_budget"}:
            stop = 6 if tamper == "over_budget" else 5
            for ordinal in range(3, stop):
                connection.execute("INSERT INTO calls SELECT ?,variant,request,response,outcome,latency_ms "
                                   "FROM calls WHERE ordinal=1", (ordinal,))
        elif tamper == "unknown_variant":
            connection.execute("UPDATE calls SET variant='invented' WHERE ordinal=1")
        else:
            connection.execute("ALTER TABLE calls ADD COLUMN changed TEXT")
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="identity|ledger"):
        evaluate_provider(sample, ledger, **args)
    assert len(client.requests) == 2
    assert ledger.read_bytes() == before


def test_campaign_one_global_budget_for_five_samples_and_durable_reuse(database, tmp_path):
    sample = load_sample(tmp_path / "archive.db", saved_failure(database, tmp_path))
    samples = [dict(deepcopy(sample), call_id=f"historical-{i}") for i in range(5)]
    client = ReferenceClient(failures=100)
    ledger = tmp_path / "campaign.db"
    args = dict(endpoint="https://api.deepseek.com", model="deepseek-flash", allow_paid_calls=True, client=client)
    report = evaluate_campaign(samples, ledger, **args)
    assert report["max_physical_calls"] == 20 and report["timeout_seconds"] == 180
    assert report["physical_calls"] == len(client.requests) == 20
    assert len(report["samples"]) == 5
    assert all(len(s["calls"]) == 4 for s in report["samples"])
    assert all(s["historical_feedback_seeded"] for s in report["samples"])
    assert all(len(r["messages"]) == 3 and r["max_tokens"] == 8192 for r in client.requests)
    assert all(json.loads(r["messages"][-1]["content"])["regeneration"] in {1, 2} for r in client.requests)
    evaluate_campaign(samples, ledger, **args)
    assert len(client.requests) == 20
    with pytest.raises(ValueError, match="identity"):
        evaluate_campaign(list(reversed(samples)), ledger, **args)
    assert len(client.requests) == 20


@pytest.mark.parametrize("tamper", ["request", "response", "table", "trigger", "manifest", "extra_calls"])
def test_campaign_fails_closed_before_new_calls(database, tmp_path, tamper):
    import sqlite3
    sample = load_sample(tmp_path / "archive.db", saved_failure(database, tmp_path))
    ledger = tmp_path / "campaign.db"
    client = ReferenceClient(failures=0)
    args = dict(endpoint="https://api.deepseek.com", model="deepseek-flash", allow_paid_calls=True, client=client)
    evaluate_campaign([sample], ledger, **args)
    assert len(client.requests) == 2
    with sqlite3.connect(ledger) as connection:
        if tamper == "request":
            connection.execute("UPDATE calls SET request='{}' WHERE ordinal=2")
        elif tamper == "response":
            connection.execute("UPDATE calls SET response='{}' WHERE ordinal=2")
        elif tamper == "table":
            connection.execute("CREATE TABLE unexpected(x TEXT)")
        elif tamper == "trigger":
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON calls BEGIN SELECT 1; END")
        elif tamper == "manifest":
            connection.execute("UPDATE manifest SET body='{}'")
        else:
            connection.execute("INSERT INTO calls SELECT 3,variant,request,response,outcome,latency_ms FROM calls WHERE ordinal=1")
    with pytest.raises(ValueError, match="ledger|experiment"):
        evaluate_campaign([sample], ledger, **args)
    assert len(client.requests) == 2


def _campaign_concurrent_unknown(samples, ledger, counter, start):
    from bili_asr.deepseek import DeepSeekError
    class Client:
        def complete(self, request, config):
            counter.put("send")
            raise DeepSeekError("simulated uncertain endpoint failure")
    start.wait(10)
    evaluate_campaign(samples, ledger, endpoint="https://api.deepseek.com", model="deepseek-flash",
                      allow_paid_calls=True, client=Client())


def test_campaign_concurrent_processes_never_resend_unknown_reservations(database, tmp_path):
    import multiprocessing
    import queue
    import sqlite3
    sample = load_sample(tmp_path / "archive.db", saved_failure(database, tmp_path))
    samples = [dict(deepcopy(sample), call_id=f"historical-{i}") for i in range(5)]
    context = multiprocessing.get_context("spawn")
    counter, start = context.Queue(), context.Event()
    ledger = tmp_path / "concurrent-campaign.db"
    processes = [context.Process(target=_campaign_concurrent_unknown, args=(samples, ledger, counter, start)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    sends = []
    while True:
        try:
            sends.append(counter.get(timeout=.1))
        except queue.Empty:
            break
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == len(sends) == 10
        assert connection.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM calls GROUP BY variant)").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM calls WHERE response IS NOT NULL").fetchone()[0] == 0


def test_prepared_chunk_and_request_cannot_change_hashed_source(database, tmp_path):
    from bili_asr.storage.editorial import EditorialRepository
    call_id = saved_failure(database, tmp_path)
    prepared = json.loads(database.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0])
    stored = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
    prepared["chunks"][0]["editable_segments"][0]["text"] = "changed-source"
    stored["request"]["messages"][1]["content"] = json.dumps(prepared["chunks"][0])
    stored["request_fingerprint"] = EditorialRepository.request_fingerprint(
        prepared["input_id"], prepared["chunks"][0]["chunk_id"], stored["request"])
    with database:
        database.execute("UPDATE editorial_inputs SET prepared_json=?", (json.dumps(prepared),))
        database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(stored),))
    with pytest.raises(ValueError, match="hashed snapshot"):
        load_sample(tmp_path / "archive.db", call_id)


def test_historical_recovery_counts_actual_followup_usage_and_censored_failures(database, tmp_path):
    from bili_asr.services.editorial_evaluation import historical_recovery_report
    call_id = saved_failure(database, tmp_path)
    report = historical_recovery_report(tmp_path / "archive.db", [call_id])
    assert report["source_failures"] == 1 and report["failures_with_followup"] == 0
    assert report["recoveries"] == 0 and report["recovery_rate_among_observed"] is None
    assert report["samples"][0]["observation"] == "censored_no_more_observed_calls"
    row = database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    response = json.loads(row["response_json"])
    body = json.loads(response["choices"][0]["message"]["content"])
    for paragraph in body["paragraphs"]:
        paragraph["issues"] = []
    response["choices"][0]["message"]["content"] = json.dumps(body)
    response["usage"] = {"prompt_tokens": 25, "completion_tokens": 12}
    with database:
        database.execute("INSERT INTO editorial_model_calls(call_id,attempt_id,input_id,chunk_id,request_json,response_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?)",
            ("actual-followup", row["attempt_id"], row["input_id"], row["chunk_id"], row["request_json"], json.dumps(response), row["started_at"] + 1, row["finished_at"] + 1))
    report = historical_recovery_report(tmp_path / "archive.db", [call_id])
    assert report["recoveries"] == 1 and report["recovery_rate_among_observed"] == 1
    assert report["additional_usage"]["prompt_tokens"]["known_total"] == 25
    assert report["additional_usage"]["reasoning_tokens"]["missing_call_counters"] == 1
    assert report["new_provider_calls"] == 0 and report["causal_feedback_improvement"] == "not_established"
