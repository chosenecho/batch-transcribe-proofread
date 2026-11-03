"""Six-slot compatible deployment scheduler; preserve the installed workflow."""

import argparse
import json
import os
import runpy
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from editorial_time_policy import gate, load_policy, time_admission


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_rows(args, sql, parameters=()):
    from contextlib import closing
    with closing(sqlite3.connect("file:" + str(args.archive_root / "archive.db") + "?mode=ro", uri=True, timeout=5)) as db:
        return db.execute(sql, parameters).fetchall()


def load_environment(args):
    sys.path.insert(0, str(args.project_root / "scripts"))
    from production import load_environment as read_environment
    os.environ.update({**read_environment(args.project_root / ".env.runtime"),
                       **read_environment(args.project_root / ".env")})
    os.chdir(args.project_root)


def admission(args):
    if (args.ops_root / "STOP").exists():
        return {"allowed": False, "reason": "service_stopping"}
    return gate(args.policy, args.ops_root / "provider-health.json", args.archive_root)


def configure_opencode_session(args, session):
    """Apply an optional loopback relay only to the OpenCode destination."""
    network = args.ops_root / "network.json"
    if not network.exists():
        session.proxies.pop("https://opencode.ai", None)
        return
    from urllib.parse import urlsplit
    proxy = json.loads(network.read_text(encoding="utf-8"))["opencode_https_proxy"]
    parsed = urlsplit(proxy)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("OpenCode relay must be an HTTP proxy on loopback")
    session.proxies["https://opencode.ai"] = proxy


def install_worker_gates(args):
    """Adapters are scoped to this child; the installed library stays untouched."""
    from bili_asr.deepseek import DeepSeekClient
    from bili_asr.editorial_runtime import EditorialWorkflowHandlers
    from bili_asr.storage.workflow import WorkflowRepository
    from bili_asr.workflow import attempt_checkpoint

    claim = WorkflowRepository.claim
    complete = DeepSeekClient.complete
    proofread = EditorialWorkflowHandlers.proofread
    active = {}

    def scheduled_claim(repository, *values, **options):
        return claim(repository, *values, **options) if admission(args)["allowed"] else None

    def scheduled_proofread(handler, job):
        active["job"] = job
        try:
            return proofread(handler, job)
        finally:
            active.pop("job", None)

    def scheduled_complete(client, request, config):
        policy = load_policy(args.policy)
        if (config.base_url.rstrip("/") != policy["provider_base_url"].rstrip("/")
                or config.model != policy["model"]):
            raise ValueError("frozen provider differs from scheduled provider")
        while not admission(args)["allowed"]:
            # The workflow's normal heartbeat continues during calendar waits.
            # Shutdown and lost leases still use its normal checkpoint contract.
            attempt_checkpoint(active["job"])
            time.sleep(1)
        attempt_checkpoint(active["job"])
        configure_opencode_session(args, client.session)
        return complete(client, request, config)

    WorkflowRepository.claim = scheduled_claim
    EditorialWorkflowHandlers.proofread = scheduled_proofread
    DeepSeekClient.complete = scheduled_complete


def probe(args):
    import requests
    load_environment(args)
    policy = load_policy(args.policy)
    started = time.monotonic()
    result = {"checked_at_epoch": time.time(), "ready": False,
              "provider_base_url": policy["provider_base_url"], "model": policy["model"]}
    key = os.environ.get("OPENCODE_API_KEY", "")
    if not key.strip():
        return {**result, "reason": "missing_opencode_key"}
    try:
        with requests.Session() as session:
            configure_opencode_session(args, session)
            response = session.get(policy["provider_base_url"].rstrip("/") + "/models",
                                   headers={"Authorization": "Bearer " + key},
                                   timeout=(8, 10), allow_redirects=False)
            result["http_status"] = response.status_code
            if response.status_code == 200:
                models = response.json().get("data", [])
                result["ready"] = any(item.get("id") == policy["model"] for item in models)
            result["reason"] = "ready" if result["ready"] else "provider_http_or_model"
    except (requests.RequestException, ValueError, AttributeError) as exc:
        result["reason"] = type(exc).__name__
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def refresh_calendars(args):
    """Import published future calendars as data, with official notice checks."""
    import re
    from urllib.parse import urlparse

    import requests
    policy = load_policy(args.policy)
    year = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).year
    refreshed = []
    for candidate in (year, year + 1):
        if str(candidate) in policy["holiday_calendars"]:
            continue
        try:
            response = requests.get(
                f"https://api.github.com/repos/NateScarlet/holiday-cn/contents/{candidate}.json",
                headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "bili-asr-calendar"},
                timeout=(5, 8))
            response.raise_for_status()
            upstream = response.json()
            if upstream["year"] != candidate:
                raise ValueError("calendar year mismatch")
            papers = upstream["papers"]
            if not 1 <= len(papers) <= 3:
                raise ValueError("calendar needs official notices")
            for url in papers:
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in {"www.gov.cn", "gov.cn"}:
                    raise ValueError("calendar notice must be from the State Council")
                notice = requests.get(url, timeout=(5, 8), allow_redirects=False)
                notice.raise_for_status()
                text = re.sub(r"<[^>]+>", "", notice.content.decode("utf-8"))
                if not all(token in text for token in (f"{candidate}年", "节假日", "国务院办公厅")):
                    raise ValueError("official notice did not confirm the year")
            days = upstream["days"]
            if not days or any(type(day["isOffDay"]) is not bool for day in days):
                raise ValueError("invalid calendar day flags")
            off = [day["date"] for day in days if day["isOffDay"]]
            if not 15 <= len(off) <= 60 or len(set(off)) != len(off):
                raise ValueError("invalid holiday leave calendar")
            policy["holiday_calendars"][str(candidate)] = {
                "source": papers[0], "off_dates": off,
                "dataset": "NateScarlet/holiday-cn", "refreshed_at_utc": datetime.now(UTC).isoformat()}
            # Validate all ISO dates and year ownership before replacing the policy.
            temporary = args.policy.with_suffix(".calendar-check")
            write_json(temporary, policy)
            load_policy(temporary)
            temporary.replace(args.policy)
            refreshed.append(candidate)
        except (requests.RequestException, ValueError, KeyError, TypeError, UnicodeError):
            # Keep the last verified calendar. An unknown year remains visible.
            policy = load_policy(args.policy)
    return {"refreshed_years": refreshed}


def execute(args):
    import fcntl
    with (args.ops_root / f"attempt-slot-{args.slot}.lock").open("a") as lock:
        # An orphaned in-flight child keeps its slot; a restarted parent cannot
        # create a seventh call while that child finishes.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        execute_workflow(args)


def execute_workflow(args):
    load_environment(args)
    install_worker_gates(args)
    sys.argv = ["bili-asr", "workflow", "run", "--archive-root", str(args.archive_root),
                "--only-editorial", "--limit", "1", "--worker-id", f"batch-ai-{args.slot}",
                "--drain-file", str(args.ops_root / "STOP"), "--drain-timeout", "180"]
    runpy.run_module("bili_asr", run_name="__main__")


def command(args, *extra):
    return [sys.executable, str(Path(__file__).resolve()), "--project-root", str(args.project_root),
            "--archive-root", str(args.archive_root), "--ops-root", str(args.ops_root),
            "--policy", str(args.policy), "--bin", str(args.bin), *extra]


def run_slot(args):
    import fcntl
    with (args.ops_root / f"slot-{args.slot}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not (args.ops_root / "STOP").exists():
            result = admission(args)
            ready = False
            if result["allowed"]:
                ready = bool(read_rows(args, "SELECT 1 FROM workflow_jobs j WHERE j.kind IN ('proofread','render_document') "
                    "AND j.status='queued' AND j.available_at<=? AND NOT EXISTS (SELECT 1 FROM workflow_job_dependencies d "
                    "JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id WHERE d.job_id=j.job_id AND p.status<>'succeeded') LIMIT 1", (int(time.time()),)))
            write_json(args.ops_root / f"slot-{args.slot}.json", {**result, "pid": os.getpid(),
                       "updated_at_epoch": time.time(), "phase": "executing" if ready else "waiting"})
            if ready:
                child = subprocess.Popen(command(args, "--slot", str(args.slot), "--execute"))
                write_json(args.ops_root / f"slot-{args.slot}.json", {**result, "pid": os.getpid(),
                           "child_pid": child.pid, "updated_at_epoch": time.time(), "phase": "executing"})
                code = child.wait()
                print(json.dumps({"slot": args.slot, "exit_code": code, "at": time.time()}), flush=True)
            time.sleep(2)


def plan_ready_parts(args, policy):
    parts = read_rows(args, "SELECT p.video_part_id FROM video_parts p JOIN videos v ON v.bvid=p.bvid "
        "WHERE v.mid=? AND EXISTS (SELECT 1 FROM workflow_jobs j WHERE j.video_part_id=p.video_part_id "
        "AND j.kind='asr' AND j.status='succeeded') AND NOT EXISTS (SELECT 1 FROM workflow_jobs j "
        "WHERE j.video_part_id=p.video_part_id AND j.kind='proofread') ORDER BY p.video_part_id LIMIT 25", (policy["uploader_mid"],))
    if not parts:
        return
    values = [str(args.bin), "workflow", "plan", "--archive-root", str(args.archive_root),
              "--proofread", "--editorial-model", policy["model"], "--editorial-base-url", policy["provider_base_url"],
              "--max-output-tokens", "65536", *policy["producer_planning_args"]]
    for part, in parts:
        values.extend(["--part-id", str(part)])
    with (args.ops_root / "planning.log").open("a") as log:
        result = subprocess.run(values, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("editorial planning failed; inspect planning.log")


def controller(args):
    import fcntl
    policy = load_policy(args.policy)
    with (args.ops_root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
            (args.ops_root / "STOP").touch()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        children, logs = {}, {}
        check, last_check, last_plan = None, 0, 0
        calendar_check, last_calendar_check = None, 0
        write_json(args.ops_root / "provider-health.json", {"checked_at_epoch": time.time(), "ready": False, "reason": "pending_probe"})
        try:
            while not stopping:
                now = time.time()
                if calendar_check is None and now - last_calendar_check >= 86400:
                    calendar_check = subprocess.Popen(command(args, "--refresh-calendars"),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    last_calendar_check = now
                if calendar_check is not None and (calendar_check.poll() is not None or now - last_calendar_check > 90):
                    if calendar_check.poll() is None:
                        calendar_check.kill()
                    calendar_check.wait()
                    calendar_check = None
                for slot in range(1, policy["workers"] + 1):
                    if slot not in children or children[slot].poll() is not None:
                        if slot in logs:
                            logs[slot].close()
                        log = logs[slot] = (args.ops_root / f"worker-{slot}.log").open("a")
                        children[slot] = subprocess.Popen(command(args, "--slot", str(slot)), stdout=log, stderr=subprocess.STDOUT)
                if check is None and now - last_check >= 60:
                    check = subprocess.Popen(command(args, "--probe"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    last_check = now
                if check is not None and (check.poll() is not None or now - last_check > 40):
                    if check.poll() is None:
                        check.kill()
                    output, _ = check.communicate()
                    try:
                        health = json.loads(output) if check.returncode == 0 else {}
                    except ValueError:
                        health = {}
                    if not health:
                        health = {"checked_at_epoch": now, "ready": False, "reason": "probe_timeout_or_failure"}
                    write_json(args.ops_root / "provider-health.json", health)
                    check = None
                if now - last_plan >= 60:
                    try:
                        plan_ready_parts(args, policy)
                    except (RuntimeError, subprocess.TimeoutExpired, sqlite3.Error) as exc:
                        # A planner error cannot disable the independent calendar guards.
                        print(json.dumps({"planning_error": type(exc).__name__, "at": now}), flush=True)
                    last_plan = now
                counts = read_rows(args, "SELECT kind,status,count(*) FROM workflow_jobs WHERE kind IN ('proofread','render_document') GROUP BY kind,status")
                state = {"updated_at_utc": datetime.now(UTC).isoformat(), "configured_workers": policy["workers"],
                         "controller_pid": os.getpid(), "slots": {slot: child.pid for slot, child in children.items()},
                         "admission": admission(args), "jobs": counts}
                write_json(args.ops_root / "state.json", state)
                time.sleep(2)
        finally:
            (args.ops_root / "STOP").touch()
            if check is not None:
                check.kill()
                check.wait()
            if calendar_check is not None:
                calendar_check.kill()
                calendar_check.wait()
            # No schedule transition sends signals to business workers.
            for child in children.values():
                child.wait()
            for log in logs.values():
                log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("project-root", "archive-root", "ops-root", "policy", "bin"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--slot", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--refresh-calendars", action="store_true")
    parser.add_argument("--at", help="Offline admission inspection with an ISO timestamp")
    args = parser.parse_args()
    if args.at:
        print(json.dumps(time_admission(load_policy(args.policy), datetime.fromisoformat(args.at))))
        return 0
    args.ops_root.mkdir(parents=True, exist_ok=True)
    if args.probe:
        print(json.dumps(probe(args)))
    elif args.refresh_calendars:
        print(json.dumps(refresh_calendars(args)))
    elif args.execute:
        execute(args)
    elif args.slot is not None:
        run_slot(args)
    else:
        controller(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
