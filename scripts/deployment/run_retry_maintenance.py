"""Run independent bounded maintenance under a container process supervisor.

Each pass uses the installed CLI and its durable ledger and health contract.
The process never retries a job itself and never initializes control state.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", default=os.environ.get("BILI_ASR_BIN"))
    for option, variable in (
        ("archive-root", "ARCHIVE_ROOT"), ("policy", "RETRY_POLICY"),
        ("state", "PRODUCTION_STATE"), ("lock", "MAINTENANCE_LOCK"),
        ("health", "MAINTENANCE_HEALTH"),
    ):
        parser.add_argument("--" + option, default=os.environ.get(variable))
    parser.add_argument("--fallback-interval-seconds", type=int, default=60)
    parser.add_argument("--tick-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    names = ("bin", "archive_root", "policy", "state", "lock", "health")
    for name in names:
        value = getattr(args, name)
        if not value or not Path(value).is_absolute():
            parser.error(name.replace("_", "-") + " requires an absolute path")
    if args.fallback_interval_seconds < 1 or args.tick_timeout_seconds < 1:
        parser.error("interval and timeout must be positive")
    command = [args.bin, "retry-maintenance", "tick"]
    for name in names[1:]:
        command.extend(["--" + name.replace("_", "-"), getattr(args, name)])
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        interval = args.fallback_interval_seconds
        try:
            policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
            candidate = policy.get("interval_seconds")
            if type(candidate) is int and candidate > 0:
                interval = candidate
        except (OSError, ValueError, AttributeError):
            # Let tick record its own failed preflight in maintenance health.
            pass
        child = None
        try:
            child = subprocess.Popen(command)
            remaining = args.tick_timeout_seconds
            while child.poll() is None and remaining > 0 and not stop.wait(1):
                remaining -= 1
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                result = "stopped" if stop.is_set() else "timeout"
            else:
                result = "completed" if child.returncode == 0 else "failed"
        except OSError as exc:
            result = type(exc).__name__
        print(json.dumps({"scheduler": "retry-maintenance", "pass": result,
                          "exit_code": child.returncode if child is not None else None,
                          "interval_seconds": interval}), flush=True)
        # Failed passes and lock skips remain visible. They do not crash-loop,
        # reset a ledger or advance the maintenance success timestamp.
        stop.wait(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
