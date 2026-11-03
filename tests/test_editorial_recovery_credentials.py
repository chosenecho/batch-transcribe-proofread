"""Offline readiness follows the same frozen provider policy as execution."""
import hashlib
import json
import socket

import pytest

from bili_asr.deepseek import DeepSeekClient, DeepSeekError
from bili_asr.editorial import EditorialConfig, canonical
from bili_asr.editorial_provider import CREDENTIAL_NAMES, editorial_provider
from bili_asr.services.archive_recovery import (
    RuntimeProbe,
    doctor_archive,
    inspect_snapshot,
)
from bili_asr.services.archive_snapshot import save_snapshot
from bili_asr.storage.database import open_database
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.workflow import WorkflowRepository
from tests.test_ai_editorial import insert_record, record
from tests.test_workflow_control_plane import _seed_part

ENDPOINTS = ("https://api.deepseek.com", "https://opencode.ai/inference/go/v1/",
             "https://opencode.ai/zen/go/v1/")
EXPECTED_CREDENTIALS = {
    ENDPOINTS[0]: "DEEPSEEK_API_KEY",
    ENDPOINTS[1]: "OPENCODE_API_KEY",
    ENDPOINTS[2]: "OPENCODE_API_KEY",
}


def seed(root, endpoints=ENDPOINTS):
    with open_database(root) as connection:
        _seed_part(connection)
        insert_record(connection, record())
        jobs = {}
        for url in endpoints:
            config = EditorialConfig(base_url=url)
            prepared = EditorialRepository(connection).prepare(1, None, config)
            job, *_ = WorkflowRepository(connection).request_editorial(video_part_id=1, input_id=prepared["input_id"])
            jobs[url] = job
    return jobs


def probe(names):
    return RuntimeProbe(frozenset(), frozenset(), frozenset({"cpu"}), frozenset(names))


@pytest.mark.parametrize("names", [(), ("DEEPSEEK_API_KEY",), ("OPENCODE_API_KEY",), ("DEEPSEEK_API_KEY", "OPENCODE_API_KEY")])
def test_frozen_provider_matrix_agrees_in_doctor_and_snapshot_without_network(tmp_path, monkeypatch, names):
    root = tmp_path / "archive"
    jobs = seed(root)
    before = hashlib.sha256((root / "archive.db").read_bytes()).hexdigest()
    def no_network(*_args, **_kwargs):
        raise AssertionError("doctor must stay offline")
    monkeypatch.setattr(socket, "create_connection", no_network)
    direct = doctor_archive(root, probe=probe(names))
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    recovered = inspect_snapshot(snapshot, probe=probe(names))
    for report in (direct, recovered):
        by_id = {job["job_id"]: job for job in report["pending_jobs"]}
        for url, identity in jobs.items():
            key = EXPECTED_CREDENTIALS[url]
            ready = key in names
            assert by_id[identity]["derived_state"] == ("ready_now" if ready else "blocked_environment")
            assert by_id[identity]["missing"] == ([] if ready else ["credential:" + key])
        assert not report["credentials_verified_online"]
    assert hashlib.sha256((root / "archive.db").read_bytes()).hexdigest() == before


@pytest.mark.parametrize("damage", ["missing-input", "bad-input", "missing-endpoint", "unknown-endpoint"])
def test_missing_or_corrupt_frozen_input_never_falls_back_to_current_default(tmp_path, damage):
    root = tmp_path / "archive"
    jobs = seed(root, (ENDPOINTS[1],))
    identity = jobs[ENDPOINTS[1]]
    with open_database(root) as connection, connection:
        if damage == "missing-input":
            connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?", (canonical({"input_id": "missing"}), identity))
        else:
            raw = connection.execute("SELECT prepared_json FROM editorial_inputs").fetchone()[0]
            prepared = json.loads(raw)
            if damage == "bad-input":
                prepared["snapshot"]["config"]["base_url"] = ENDPOINTS[0]
                connection.execute("UPDATE editorial_inputs SET prepared_json=?", (canonical(prepared),))
            else:
                # A not-yet-bound automatic chain uses its own frozen config.
                config = prepared["snapshot"]["config"]
                if damage == "missing-endpoint":
                    del config["base_url"]
                else:
                    config["base_url"] = "https://private.invalid/?secret=must-not-enter-report"
                connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?",
                                   (canonical({"asr_job_id": "future-asr", "editorial_config": config}), identity))
    report = doctor_archive(root, probe=probe(CREDENTIAL_NAMES))
    job = next(item for item in report["pending_jobs"] if item["job_id"] == identity)
    assert job["derived_state"] == "blocked_environment"
    assert job["missing"] == ["editorial:invalid-frozen-config"]
    assert "must-not-enter-report" not in json.dumps(report)


@pytest.mark.parametrize("url", ENDPOINTS)
def test_automatic_asr_chain_uses_payload_endpoint_before_freezing_input(tmp_path, url):
    root = tmp_path / "archive"
    jobs = seed(root, (url,))
    identity = jobs[url]
    with open_database(root) as connection, connection:
        config = EditorialConfig(base_url=url).to_dict()
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?",
                           (canonical({"asr_job_id": "future-asr", "editorial_config": config}), identity))
    key = EXPECTED_CREDENTIALS[url]
    for names in ((), (key,)):
        report = doctor_archive(root, probe=probe(names))
        job = next(item for item in report["pending_jobs"] if item["job_id"] == identity)
        assert job["missing"] == ([] if names else ["credential:" + key])


def test_local_probe_detects_both_key_names_and_never_reports_values(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    seed(root)
    for key in CREDENTIAL_NAMES:
        monkeypatch.setenv(key, "private-" + key)
    local = RuntimeProbe.local()
    assert CREDENTIAL_NAMES == frozenset({"DEEPSEEK_API_KEY", "OPENCODE_API_KEY"})
    assert CREDENTIAL_NAMES <= local.credential_names
    encoded = json.dumps(doctor_archive(root, probe=local))
    assert "private-DEEPSEEK_API_KEY" not in encoded
    assert "private-OPENCODE_API_KEY" not in encoded


@pytest.mark.parametrize("url", ENDPOINTS)
def test_client_missing_key_uses_same_policy_and_explicit_key_still_wins(monkeypatch, url):
    key = EXPECTED_CREDENTIALS[url]
    assert editorial_provider(url).credential_name == key
    monkeypatch.delenv(key, raising=False)
    class OfflineSession:
        calls = 0
        def post(self, _url, *, headers, **_kwargs):
            self.calls += 1
            assert headers["Authorization"] == "Bearer explicit-value"
            class Response:
                status_code = 200
                def json(self):
                    return {"offline": True}
            return Response()
    session = OfflineSession()
    client = DeepSeekClient(session=session)
    with pytest.raises(DeepSeekError, match=key + " is not configured"):
        client.complete({}, EditorialConfig(base_url=url))
    assert session.calls == 0
    assert DeepSeekClient("explicit-value", session=session).complete({}, EditorialConfig(base_url=url)) == {"offline": True}
