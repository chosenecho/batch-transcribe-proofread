"""CLI and provider failures keep frozen inputs and physical reservations intact."""
import json

import pytest

from bili_asr import cli
from bili_asr.deepseek import DeepSeekClient, DeepSeekError, request_body
from bili_asr.editorial import EditorialConfig, digest
from bili_asr.editorial_provider import editorial_provider
from bili_asr.services.editorial_comparison import compare_presets
from bili_asr.storage.editorial_replay import (
    ReplayEvidenceError,
    objects,
    require_indexes,
)
from tests.test_ai_editorial import database as editorial_database
from tests.test_editorial_comparison import ENDPOINT, MODEL, Provider, source
from tests.test_editorial_regeneration import ReferenceClient, queued

database = editorial_database


@pytest.mark.parametrize("endpoint", [None, False, {}, "http://api.deepseek.com",
    "https://api.deepseek.com.attacker.test", "https://opencode.ai/zen/go/v2",
    "https://opencode.ai/inference/go/v2"])
def test_unknown_endpoint_never_selects_a_credential(endpoint):
    with pytest.raises(ValueError, match="unsupported editorial API endpoint"):
        editorial_provider(endpoint)


@pytest.mark.parametrize("endpoint,required,unrelated", [
    ("https://api.deepseek.com/", "DEEPSEEK_API_KEY", "OPENCODE_API_KEY"),
    ("https://opencode.ai/inference/go/v1/", "OPENCODE_API_KEY", "DEEPSEEK_API_KEY"),
    ("https://opencode.ai/zen/go/v1/", "OPENCODE_API_KEY", "DEEPSEEK_API_KEY"),
])
def test_unrelated_credential_cannot_authorize_an_endpoint(monkeypatch, endpoint, required, unrelated):
    class ForbiddenTransport:
        def post(self, *args, **kwargs):
            pytest.fail("an endpoint without its credential must not send HTTP")
    monkeypatch.delenv(required, raising=False)
    monkeypatch.setenv(unrelated, "offline-unrelated-key")
    with pytest.raises(DeepSeekError) as error:
        DeepSeekClient(session=ForbiddenTransport()).complete({}, EditorialConfig(base_url=endpoint.rstrip("/")))
    assert error.value.error_code == "deepseek_missing_api_key"
    assert error.value.safe_details["reason_code"] == "missing_api_key"
    assert editorial_provider(endpoint).credential_name == required


def test_integrity_cli_lists_damage_without_rewriting_calls_and_index_cli_recovers(database, tmp_path, capsys):
    workflow, repository, _, _, prepared, _ = queued(database, tmp_path, ReferenceClient())
    claimed = workflow.claim("integrity-fixture")
    chunk = prepared["chunks"][0]
    call = repository.begin_call(claimed, prepared["input_id"], chunk["chunk_id"], request_body(chunk, EditorialConfig()))
    repository.finish_call(call, {})
    with database:
        database.execute("UPDATE editorial_model_calls SET response_json='{' WHERE call_id=?", (call,))
    original = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")]
    assert cli.main(["editorial", "call-integrity", "--archive-root", str(tmp_path), "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["invalid_json_call_ids"] == [call]
    assert cli.main(["editorial", "call-integrity", "--archive-root", str(tmp_path), "--limit", "0"]) == 1
    assert "limit must be between" in capsys.readouterr().err
    assert cli.main(["editorial", "replay-index", "--archive-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["content_rows_changed"] == 0
    assert require_indexes(database)
    with database:
        database.execute("DROP INDEX " + next(iter(objects())))
    assert cli.main(["editorial", "replay-index", "--archive-root", str(tmp_path)]) == 1
    assert "incomplete derived index group" in capsys.readouterr().err
    assert cli.main(["editorial", "replay-index", "--archive-root", str(tmp_path), "--recover"]) == 0
    capsys.readouterr()
    assert require_indexes(database)
    assert [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")] == original


def test_explicit_large_plan_and_review_cli_bind_evidence_without_model_or_archive_mutation(
        database, tmp_path, capsys):
    _, _, _, _, prepared, _ = queued(database, tmp_path, ReferenceClient())
    archive_before = (tmp_path / "archive.db").read_bytes()
    assert cli.main(["editorial", "plan", "--archive-root", str(tmp_path), "--input-id", prepared["input_id"],
                     "--preset", "large-v1"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["physical_calls"] == 0 and len(plan["plans"]) == 1
    assert (tmp_path / "archive.db").read_bytes() == archive_before
    ledger, packet, review_path, output = [tmp_path / name for name in ("compare.db", "packet.json", "review.json", "record.json")]
    provider = Provider()
    compare_presets(source(), ledger, endpoint=ENDPOINT, model=MODEL, allow_paid_calls=True,
                   client=provider, maximum=4)
    assert len(provider.calls) == 2
    assert cli.main(["editorial", "compare-review", "--ledger", str(ledger), "--out", str(packet)]) == 0
    capsys.readouterr()
    review = json.loads(packet.read_text(encoding="utf-8"))["review_template"]
    review.update(reviewer_kind="agent", reviewer="offline-boundary-review", reviewed_at="2026-10-13T12:00:00+08:00")
    for rubric in review["rubric"].values():
        for entry in rubric.values():
            entry.update(verdict="unknown", notes="offline fixtures prove structure only; semantics need human evidence")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    command = ["editorial", "record-review", "--ledger", str(ledger), "--review", str(review_path), "--out", str(output)]
    assert cli.main(command) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["human_review_status"] == "not_reviewed" and record["review_sha256"] == digest(review)
    original = output.read_bytes()
    assert cli.main(command) == 1
    capsys.readouterr()
    assert output.read_bytes() == original and (tmp_path / "archive.db").read_bytes() == archive_before
    assert len(provider.calls) == 2


@pytest.mark.parametrize("damage,reason", [("invocation_kind", "invocation_kind_invalid"),
                                         ("response_array", "response_json_invalid")])
def test_ambiguous_call_shapes_refuse_recovery_reservation_without_rewriting_evidence(database, tmp_path, damage, reason):
    workflow, repository, _, _, prepared, _ = queued(database, tmp_path, ReferenceClient())
    claimed = workflow.claim("damaged-call-fixture")
    chunk = prepared["chunks"][0]
    call = repository.begin_call(claimed, prepared["input_id"], chunk["chunk_id"], request_body(chunk, EditorialConfig()))
    repository.finish_call(call, {})
    with database:
        if damage == "invocation_kind":
            stored = json.loads(database.execute("SELECT request_json FROM editorial_model_calls").fetchone()[0])
            stored["invocation_kind"] = "free_provider"
            database.execute("UPDATE editorial_model_calls SET request_json=?", (json.dumps(stored),))
        else:
            database.execute("UPDATE editorial_model_calls SET response_json='[]'")
    before = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")]
    with pytest.raises(ReplayEvidenceError) as error:
        repository.begin_call(claimed, prepared["input_id"], chunk["chunk_id"], {}, recovery={"max_physical_calls": 3})
    assert error.value.safe_details["reason_code"] == reason and error.value.safe_details["call_id"] == call
    assert [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls")] == before
