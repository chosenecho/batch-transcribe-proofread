"""Notify about upstream commit heads without publishing private Git metadata."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_START = "<!-- upstream-monitor-state:v1 "
STATE_END = " -->"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DIGEST = re.compile(r"[0-9a-f]{64}")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


class MonitorError(RuntimeError):
    """A sanitized error safe for a public workflow log."""


def fingerprints(remote_output: str) -> list[str]:
    """Discard ref names and replace private commit IDs with one-way digests."""
    heads = set()
    for line in remote_output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", parts[0]):
            raise MonitorError("Invalid upstream response")
        if not parts[1].startswith("refs/heads/"):
            raise MonitorError("Unexpected upstream ref type")
        heads.add(hashlib.sha256(parts[0].lower().encode("ascii")).hexdigest())
    if not heads or len(heads) > 500:
        raise MonitorError("Upstream returned no heads or too many heads")
    return sorted(heads)


def observe(upstream: str) -> list[str]:
    if not REPOSITORY.fullmatch(upstream):
        raise MonitorError("Invalid upstream repository")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", f"git@github.com:{upstream}.git"],
            capture_output=True, text=True, timeout=90, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MonitorError("Upstream access failed; check the read-only deploy key") from None
    if result.returncode:
        # Do not expose private refs, SSH diagnostics, or upstream output.
        raise MonitorError("Upstream access failed; check the read-only deploy key")
    return fingerprints(result.stdout)


def state_text(heads: list[str], observed_at: str) -> str:
    value = {"version": 1, "heads": sorted(set(heads)), "observed_at": observed_at}
    return STATE_START + json.dumps(value, sort_keys=True, separators=(",", ":")) + STATE_END


def read_state(body: str) -> dict:
    if body.count(STATE_START) != 1:
        raise MonitorError("Tracking issue has no unique state marker")
    start = body.index(STATE_START) + len(STATE_START)
    end = body.find(STATE_END, start)
    if end == -1:
        raise MonitorError("Tracking issue state is incomplete")
    try:
        state = json.loads(body[start:end])
    except ValueError:
        raise MonitorError("Tracking issue state is invalid") from None
    if (not isinstance(state, dict) or set(state) != {"version", "heads", "observed_at"}
            or type(state["version"]) is not int or state["version"] != 1
            or not isinstance(state["heads"], list) or not 1 <= len(state["heads"]) <= 500
            or any(not isinstance(head, str) or not DIGEST.fullmatch(head) for head in state["heads"])
            or not isinstance(state["observed_at"], str)):
        raise MonitorError("Tracking issue state is invalid")
    return state


def replace_state(body: str, heads: list[str], observed_at: str) -> str:
    read_state(body)
    start = body.index(STATE_START)
    end = body.index(STATE_END, start) + len(STATE_END)
    return body[:start] + state_text(heads, observed_at) + body[end:]


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request("https://api.github.com" + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "batch-transcribe-upstream-monitor",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise MonitorError(f"GitHub API failed with HTTP {error.code}") from None
        except (URLError, OSError, ValueError):
            raise MonitorError("GitHub API request failed") from None


def has_comment(api, issue_path: str, marker: str) -> bool:
    page = 1
    while True:
        comments = api.request("GET", f"{issue_path}/comments?per_page=100&page={page}")
        if not isinstance(comments, list):
            raise MonitorError("Invalid tracking issue comments")
        if any(marker in (comment.get("body") or "") for comment in comments):
            return True
        if len(comments) < 100:
            return False
        page += 1


def monitor(api, *, repository: str, issue: int, heads: list[str],
            notify: list[str], now: str, test_run: str | None = None) -> str:
    if not REPOSITORY.fullmatch(repository) or type(issue) is not int or issue <= 0:
        raise MonitorError("Invalid downstream repository or tracking issue")
    if any(not LOGIN.fullmatch(login) for login in notify):
        raise MonitorError("Invalid notification recipient")
    if (not heads or len(heads) > 500
            or any(not isinstance(head, str) or not DIGEST.fullmatch(head) for head in heads)):
        raise MonitorError("Invalid observed state")
    path = f"/repos/{repository}/issues/{issue}"
    issue_data = api.request("GET", path)
    if "pull_request" in issue_data:
        raise MonitorError("Configured tracking issue is a pull request")
    body = issue_data.get("body") or ""
    previous = read_state(body)
    changed = set(heads) != set(previous["heads"])
    added = set(heads) - set(previous["heads"])
    mentions = " ".join("@" + login for login in notify)
    result = "unchanged"
    if added:
        event = hashlib.sha256("".join(sorted(set(heads))).encode("ascii")).hexdigest()
        marker = f"<!-- upstream-monitor-event:{event} -->"
        if not has_comment(api, path, marker):
            api.request("POST", path + "/comments", {"body":
                f"{mentions} 上游检测到新的分支提交头，请检查是否需要同步处理链改动。"
                f"\n\n检测时间：{now}。为保护私有上游，此处不公开分支名、提交 ID、提交消息或代码。"
                f"\n\n{marker}"})
        result = "notified"
    if changed:
        # Post first. A failed state write can safely retry using the event marker.
        api.request("PATCH", path, {"body": replace_state(body, heads, now)})
        if not added:
            result = "state-updated-without-notification"
    if test_run is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", test_run):
            raise MonitorError("Invalid test run identifier")
        marker = f"<!-- upstream-monitor-test:{test_run} -->"
        if not has_comment(api, path, marker):
            api.request("POST", path + "/comments", {"body":
                f"{mentions} 上游监测已启用，这是通知通道的测试消息，不表示上游出现新提交。"
                f"\n\n检测时间：{now}。后续只有检测到更新才会发布提醒。\n\n{marker}"})
        result += "+test-notification"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".github/upstream-monitor.json"))
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise MonitorError("GH_TOKEN is required")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        result = monitor(GitHub(token), repository=repository, issue=config["issue"],
            heads=observe(config["upstream"]), notify=config["notify"], now=now,
            test_run=os.environ.get("GITHUB_RUN_ID", "manual") if args.test_notification else None)
        print("Upstream monitor: " + result)
        return 0
    except MonitorError as error:
        print("Upstream monitor: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("Upstream monitor: invalid configuration or response", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
