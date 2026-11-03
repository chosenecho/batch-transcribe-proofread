"""Calendar transitions must guard actual claims and every paid model request."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOYMENT = Path(__file__).resolve().parents[1] / "scripts" / "deployment"
sys.path.insert(0, str(DEPLOYMENT))
from editorial_time_policy import gate, time_admission  # noqa: E402

spec = importlib.util.spec_from_file_location("editorial_schedule_runner", DEPLOYMENT / "run_editorial_schedule.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def policy():
    return {"schema": "editorial-time-policy/v1", "timezone": "Asia/Shanghai", "workers": 6,
            "weekday_blackout_minutes": [[540, 720], [840, 1080]],
            "holiday_calendars": {"2026": {"source": "https://www.gov.cn/notice", "off_dates": ["2026-10-01"]}},
            "provider_base_url": "https://opencode.ai/inference/go/v1", "model": "deepseek-v4.1-flash"}


@pytest.mark.parametrize(("stamp", "allowed"), [
    ("2026-10-14T08:59:59+08:00", True), ("2026-10-14T09:00:00+08:00", False),
    ("2026-10-14T11:59:59+08:00", False), ("2026-10-14T12:00:00+08:00", True),
    ("2026-10-14T13:59:59+08:00", True), ("2026-10-14T14:00:00+08:00", False),
    ("2026-10-14T17:59:59+08:00", False), ("2026-10-14T18:00:00+08:00", True),
    ("2026-10-14T01:00:00+00:00", False), ("2026-10-01T10:00:00+08:00", True),
    ("2026-10-17T10:00:00+08:00", True), ("2026-10-18T15:00:00+08:00", True),
    ("2026-10-10T10:00:00+08:00", True),  # Saturday makeup work is still a weekend.
])
def test_beijing_boundaries_holidays_and_weekends(policy, stamp, allowed):
    assert time_admission(policy, datetime.fromisoformat(stamp))["allowed"] is allowed


def test_unknown_calendar_keeps_weekday_guard_and_reports_it(policy):
    result = time_admission(policy, datetime.fromisoformat("2027-01-04T10:00:00+08:00"))
    assert not result["allowed"]
    assert not result["calendar_year_known"]


def test_opencode_relay_routes_only_the_provider_host(tmp_path):
    import requests
    args = SimpleNamespace(ops_root=tmp_path)
    (tmp_path / "network.json").write_text(json.dumps({"opencode_https_proxy": "http://127.0.0.1:18443"}))
    with requests.Session() as session:
        runner.configure_opencode_session(args, session)
        assert requests.utils.select_proxy("https://opencode.ai/inference/go/v1/models", session.proxies) == "http://127.0.0.1:18443"
        assert requests.utils.select_proxy("https://www.gov.cn/notice", session.proxies) is None
        assert requests.utils.select_proxy("https://api.deepseek.com/chat/completions", session.proxies) is None


def test_removing_network_config_clears_relay_for_existing_session(tmp_path):
    import requests
    args = SimpleNamespace(ops_root=tmp_path)
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"opencode_https_proxy": "http://127.0.0.1:18443"}))
    with requests.Session() as session:
        session.proxies["https://another.example"] = "http://127.0.0.1:9999"
        runner.configure_opencode_session(args, session)
        network.unlink()
        runner.configure_opencode_session(args, session)
        assert "https://opencode.ai" not in session.proxies
        assert session.proxies["https://another.example"] == "http://127.0.0.1:9999"


@pytest.mark.parametrize("proxy", ["http://example.com:18443", "https://127.0.0.1:18443",
                                    "http://user:secret@127.0.0.1:18443", "http://127.0.0.1:18443/path"])
def test_opencode_relay_rejects_non_loopback_or_credential_urls(tmp_path, proxy):
    (tmp_path / "network.json").write_text(json.dumps({"opencode_https_proxy": proxy}))
    with pytest.raises(ValueError, match="loopback"):
        runner.configure_opencode_session(SimpleNamespace(ops_root=tmp_path), SimpleNamespace(proxies={}))


@pytest.mark.parametrize(("status", "models", "ready"), [
    (200, [{"id": "deepseek-v4.1-flash"}], True),
    (200, [{"id": "other-model"}], False),
    (503, [], False),
])
def test_health_probe_uses_relay_and_requires_available_model(tmp_path, policy, monkeypatch, status, models, ready):
    import requests

    config = tmp_path / "policy.json"
    config.write_text(json.dumps(policy), encoding="utf-8")
    (tmp_path / "network.json").write_text(json.dumps({"opencode_https_proxy": "http://127.0.0.1:18443"}))
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setattr(runner, "load_environment", lambda _: None)
    calls = []

    def get(session, url, **options):
        assert session.proxies == {"https://opencode.ai": "http://127.0.0.1:18443"}
        assert options["allow_redirects"] is False
        assert options["headers"] == {"Authorization": "Bearer test-key"}
        calls.append(url)
        return SimpleNamespace(status_code=status, json=lambda: {"data": models})

    monkeypatch.setattr(requests.Session, "get", get)
    report = runner.probe(SimpleNamespace(policy=config, ops_root=tmp_path))
    assert report["ready"] is ready
    assert report["http_status"] == status
    assert calls == [policy["provider_base_url"] + "/models"]
    assert "test-key" not in json.dumps(report)


def test_gate_blocks_stale_wrong_provider_and_low_disk(tmp_path, policy, monkeypatch):
    import shutil
    config = tmp_path / "policy.json"
    health = tmp_path / "health.json"
    config.write_text(json.dumps(policy), encoding="utf-8")
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    snapshot = {"checked_at_epoch": now.timestamp(), "ready": True,
                "provider_base_url": policy["provider_base_url"], "model": policy["model"]}
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * 1024 ** 3))
    health.write_text(json.dumps(snapshot), encoding="utf-8")
    assert gate(config, health, tmp_path, now=now)["allowed"]
    for replacement in ({"checked_at_epoch": now.timestamp() - 121}, {"ready": False}, {"model": "another-model"}):
        health.write_text(json.dumps({**snapshot, **replacement}), encoding="utf-8")
        assert gate(config, health, tmp_path, now=now)["reason"] == "provider_unavailable"
    health.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=5 * 1024 ** 3))
    assert gate(config, health, tmp_path, now=now)["reason"] == "disk_below_6GiB"
    config.write_text("{}", encoding="utf-8")
    assert not gate(config, health, tmp_path, now=now)["allowed"]


def test_claim_and_each_model_call_wait_for_admission(tmp_path, policy, monkeypatch):
    from bili_asr import workflow
    from bili_asr.deepseek import DeepSeekClient
    from bili_asr.editorial_runtime import EditorialWorkflowHandlers
    from bili_asr.storage.workflow import WorkflowRepository

    config = tmp_path / "policy.json"
    config.write_text(json.dumps(policy), encoding="utf-8")
    args = SimpleNamespace(policy=config, ops_root=tmp_path)
    opened = False
    claims, calls, checkpoints, waits = [], [], [], []
    monkeypatch.setattr(runner, "admission", lambda _: {"allowed": opened})
    monkeypatch.setattr(workflow, "attempt_checkpoint", lambda job: checkpoints.append(job))
    monkeypatch.setattr(WorkflowRepository, "claim", lambda *_args, **_kwargs: claims.append("claim") or "job")
    monkeypatch.setattr(DeepSeekClient, "complete", lambda *_args: calls.append("paid-call") or {"ok": True})

    def resume(_):
        nonlocal opened
        assert not calls or len(calls) == 1
        waits.append("wait")
        opened = True

    monkeypatch.setattr(runner.time, "sleep", resume)
    request_config = SimpleNamespace(base_url=policy["provider_base_url"], model=policy["model"])
    client = SimpleNamespace(session=SimpleNamespace(proxies={}))

    def proofread(_handler, job):
        nonlocal opened
        DeepSeekClient.complete(client, {}, request_config)
        opened = False  # Calendar can close between chunks in the same task.
        DeepSeekClient.complete(client, {}, request_config)
        return job

    monkeypatch.setattr(EditorialWorkflowHandlers, "proofread", proofread)
    runner.install_worker_gates(args)
    assert WorkflowRepository.claim(None) is None
    assert claims == []
    opened = True
    assert WorkflowRepository.claim(None) == "job"
    opened = False
    assert EditorialWorkflowHandlers.proofread(None, "active-job") == "active-job"
    assert calls == ["paid-call", "paid-call"]
    assert waits == ["wait", "wait"]
    assert checkpoints == ["active-job"] * 4


def test_wrong_frozen_model_rejected_before_paid_call(tmp_path, policy, monkeypatch):
    from bili_asr.deepseek import DeepSeekClient
    from bili_asr.editorial_runtime import EditorialWorkflowHandlers
    from bili_asr.storage.workflow import WorkflowRepository
    config = tmp_path / "policy.json"
    config.write_text(json.dumps(policy), encoding="utf-8")
    calls = []
    monkeypatch.setattr(WorkflowRepository, "claim", lambda *_: None)
    monkeypatch.setattr(EditorialWorkflowHandlers, "proofread", lambda *_: None)
    monkeypatch.setattr(DeepSeekClient, "complete", lambda *_: calls.append("paid-call"))
    runner.install_worker_gates(SimpleNamespace(policy=config))
    with pytest.raises(ValueError, match="frozen provider"):
        DeepSeekClient.complete(None, {}, SimpleNamespace(base_url=policy["provider_base_url"], model="wrong"))
    assert calls == []


@pytest.mark.parametrize("notice_host", ["www.gov.cn", "untrusted.example"])
def test_calendar_refresh_requires_official_notice_before_policy_replace(tmp_path, policy, monkeypatch, notice_host):
    import requests

    config = tmp_path / "policy.json"
    original = json.dumps(policy)
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(runner, "datetime", SimpleNamespace(now=lambda _: datetime(2026, 10, 14, tzinfo=UTC)))
    dataset = {"year": 2027, "papers": [f"https://{notice_host}/notice"],
               "days": [{"date": f"2027-01-{n:02}", "isOffDay": True} for n in range(1, 16)]}
    fetched = []

    def get(url, **_):
        fetched.append(url)
        if "api.github.com" in url:
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: dataset)
        assert notice_host == "www.gov.cn"
        return SimpleNamespace(raise_for_status=lambda: None,
                               content="<html>国务院办公厅关于2027年节假日安排的通知</html>".encode())

    monkeypatch.setattr(requests, "get", get)
    report = runner.refresh_calendars(SimpleNamespace(policy=config))
    if notice_host == "www.gov.cn":
        assert report["refreshed_years"] == [2027]
        assert len(json.loads(config.read_text())["holiday_calendars"]["2027"]["off_dates"]) == 15
    else:
        assert report["refreshed_years"] == []
        assert config.read_text() == original
        assert len(fetched) == 1
