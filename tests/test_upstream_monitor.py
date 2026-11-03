"""Offline tests for update detection, redaction, and retry-safe notifications."""
from copy import deepcopy
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import upstream_monitor as monitor

NOW = "2026-11-04T00:00:00+00:00"
OLD = "a" * 64
NEW = "b" * 64


class FakeGitHub:
    def __init__(self, heads=(OLD,)):
        self.body = "Keep this description.\n" + monitor.state_text(list(heads), NOW)
        self.comments = []
        self.writes = []
        self.fail_patch_once = False

    def request(self, method, path, payload=None):
        if method == "GET" and "/comments?" in path:
            page = int(path.rsplit("=", 1)[1])
            return deepcopy(self.comments[(page - 1) * 100:page * 100])
        if method == "GET":
            return {"body": self.body}
        self.writes.append((method, path, payload))
        if method == "POST":
            self.comments.append(deepcopy(payload))
            return payload
        if method == "PATCH":
            if self.fail_patch_once:
                self.fail_patch_once = False
                raise monitor.MonitorError("Transient state update failure")
            self.body = payload["body"]
            return payload
        raise AssertionError("Unexpected API request")


def check(api, heads, **kwargs):
    return monitor.monitor(api, repository="example/processing", issue=1,
        heads=heads, notify=["example-owner"], now=NOW, **kwargs)


class UpstreamMonitorTests(unittest.TestCase):
    def test_remote_heads_are_redacted_and_deduplicated(self):
        raw_id = "1" * 40
        raw = f"{raw_id}\trefs/heads/private-branch\n{raw_id}\trefs/heads/main\n"
        heads = monitor.fingerprints(raw)
        self.assertEqual(heads, [hashlib.sha256(raw_id.encode()).hexdigest()])
        self.assertNotIn(raw_id, json.dumps(heads))
        self.assertNotIn("private-branch", json.dumps(heads))

    def test_empty_or_invalid_remote_never_advances_state(self):
        for value in ("", "bad refs/heads/main", "a" * 40 + " refs/tags/tag"):
            with self.subTest(value=value), self.assertRaises(monitor.MonitorError):
                monitor.fingerprints(value)

    def test_no_change_produces_no_write(self):
        api = FakeGitHub()
        self.assertEqual(check(api, [OLD]), "unchanged")
        self.assertEqual(api.writes, [])

    def test_new_commit_notifies_then_advances_state(self):
        api = FakeGitHub()
        self.assertEqual(check(api, [NEW]), "notified")
        self.assertEqual([call[0] for call in api.writes], ["POST", "PATCH"])
        self.assertEqual(monitor.read_state(api.body)["heads"], [NEW])
        self.assertTrue(api.body.startswith("Keep this description."))
        self.assertIn("@example-owner", api.comments[0]["body"])

    def test_repeated_observation_does_not_repeat_notification(self):
        api = FakeGitHub()
        check(api, [NEW])
        self.assertEqual(check(api, [NEW]), "unchanged")
        self.assertEqual(len(api.comments), 1)

    def test_an_additional_branch_head_also_notifies(self):
        api = FakeGitHub()
        self.assertEqual(check(api, [OLD, NEW]), "notified")
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(monitor.read_state(api.body)["heads"], [OLD, NEW])

    def test_state_failure_does_not_duplicate_delivered_comment(self):
        api = FakeGitHub()
        api.fail_patch_once = True
        with self.assertRaises(monitor.MonitorError):
            check(api, [NEW])
        self.assertEqual(monitor.read_state(api.body)["heads"], [OLD])
        check(api, [NEW])
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(monitor.read_state(api.body)["heads"], [NEW])

    def test_deleted_branch_is_silent(self):
        api = FakeGitHub([OLD, NEW])
        self.assertEqual(check(api, [OLD]), "state-updated-without-notification")
        self.assertEqual(api.comments, [])
        self.assertEqual(monitor.read_state(api.body)["heads"], [OLD])

    def test_comment_pagination_preserves_deduplication(self):
        api = FakeGitHub()
        check(api, [NEW])
        existing = api.comments.pop()
        api.body = monitor.state_text([OLD], NOW)
        api.comments = [{"body": "unrelated"} for _ in range(100)] + [existing]
        check(api, [NEW])
        self.assertEqual(len(api.comments), 101)

    def test_private_remote_metadata_never_reaches_issue_api(self):
        raw_id = "2" * 40
        private_branch = "private-topic-name"
        heads = monitor.fingerprints(f"{raw_id} refs/heads/{private_branch}")
        api = FakeGitHub()
        check(api, heads)
        written = json.dumps(api.writes)
        self.assertNotIn(raw_id, written)
        self.assertNotIn(private_branch, written)

    def test_corrupt_or_missing_state_fails_without_writes(self):
        for body in ("manual text", monitor.STATE_START + "{}" + monitor.STATE_END):
            api = FakeGitHub()
            api.body = body
            with self.assertRaises(monitor.MonitorError):
                check(api, [NEW])
            self.assertEqual(api.writes, [])

    def test_explicit_test_is_distinct_and_idempotent(self):
        api = FakeGitHub()
        self.assertEqual(check(api, [OLD], test_run="1234"), "unchanged+test-notification")
        check(api, [OLD], test_run="1234")
        self.assertEqual(len(api.comments), 1)
        self.assertIn("测试消息", api.comments[0]["body"])
        self.assertNotIn("upstream-monitor-event:", api.comments[0]["body"])
        self.assertEqual(monitor.read_state(api.body)["heads"], [OLD])

    def test_ssh_failure_does_not_print_captured_private_details(self):
        result = subprocess.CompletedProcess([], 1, stdout="private-sha", stderr="private-error")
        with patch.object(monitor.subprocess, "run", return_value=result):
            with self.assertRaises(monitor.MonitorError) as failure:
                monitor.observe("example/private-upstream")
        self.assertNotIn("private-sha", str(failure.exception))
        self.assertNotIn("private-error", str(failure.exception))

    def test_api_failure_does_not_reveal_response_payload(self):
        failure = HTTPError("https://api.github.com", 403, "private-response", {}, None)
        with patch.object(monitor, "urlopen", side_effect=failure):
            with self.assertRaises(monitor.MonitorError) as raised:
                monitor.GitHub("not-a-real-token").request("GET", "/repos/example/private")
        self.assertEqual(str(raised.exception), "GitHub API failed with HTTP 403")


if __name__ == "__main__":
    unittest.main()
