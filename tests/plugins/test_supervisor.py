"""Synthetic adapters and isolated subprocess crash fixtures. No external endpoints."""

import base64
import hashlib
import http.client
import http.server
import hmac
import io
import json
import os
import signal
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

import sandbox

PLUGIN = Path(__file__).resolve().parents[2] / "mypeople/runtime/plugins/github-prs"
HEAD = "a" * 40
NEXT = "b" * 40
BASE = "c" * 40
MERGE = "d" * 40
REPO = "owner/repo"


def snapshot(head=HEAD, *, review=12, check=(7, 1), merged=False, closed=False, threads=()):
    return {
        "head": head,
        "base": BASE,
        "open": not (merged or closed),
        "draft": False,
        "merged": MERGE if merged else "",
        "approval": review,
        "checks": [list(check)] if check else [],
        "threads": list(threads),
        "copilot": "none",
    }


class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(sys, "path", [str(PLUGIN), *sys.path]):
            import supervisor_ledger
            import supervisor
            import supervisor_adapters
            import supervisor_service

        cls.module = supervisor
        cls.ledger = supervisor_ledger
        cls.adapters = supervisor_adapters
        cls.service = supervisor_service

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.db"
        self.now = 100.0
        self.db = self.ledger.Ledger(self.path, lambda: self.now)

    def test_source_retry_after_cursor_commit_is_idempotent(self):
        event = {"id": "1" * 32, "card": "a1", "at": 101}
        self.assertEqual([event["id"]], self.db.ingest_board([event]))
        reopened = self.ledger.Ledger(self.path, lambda: self.now)
        self.assertEqual([event["id"]], reopened.ingest_board([event]))
        self.assertEqual({"pending": 1}, reopened.status()["actions"])
        self.assertEqual(event["id"], reopened.status()["journal_cursor"])
        self.assertEqual("todo_done", reopened.claim()["kind"])

    def test_expired_lease_cannot_dispatch_and_inflight_is_not_reassigned(self):
        self.db.ingest_board([{"id": "1" * 32, "card": "a1", "at": self.now}])
        stale = self.db.claim()
        self.now += 61
        live = self.db.claim()
        self.assertIsNotNone(live)
        self.assertFalse(self.db.begin(stale))
        self.assertTrue(self.db.begin(live))
        self.now += 121
        self.assertIsNone(self.db.claim())
        self.assertEqual({"unknown": 1}, self.db.status()["actions"])

    def test_webhook_signature_allowlist_and_delivery_dedupe(self):
        raw = json.dumps(
            {
                "action": "synchronize",
                "repository": {"full_name": REPO},
                "number": 4,
                "pull_request": {"head": {"sha": HEAD}},
            }
        ).encode()
        signature = "sha256=" + hmac.new(b"fixture", raw, hashlib.sha256).hexdigest()
        self.assertRaises(
            ValueError,
            self.module.ingest_webhook,
            self.db,
            {REPO},
            b"fixture",
            "sha256=wrong",
            "pull_request",
            "d1",
            raw,
        )
        for _ in range(2):
            self.module.ingest_webhook(self.db, {REPO}, b"fixture", signature, "pull_request", "d1", raw)
        self.assertEqual([(REPO, 4, 1)], self.db.tracked(dirty=True))
        self.assertEqual(1, self.db.status()["webhooks"])

    def test_current_head_snapshot_cancels_stale_ready_even_on_same_sha(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot())
        ready = self.db.claim()
        self.assertEqual("ready", ready["kind"])
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, self.db.revision(REPO, 4), snapshot(review=None))
        self.assertFalse(self.db.begin(ready))
        self.assertIsNone(self.db.claim())

    def test_sent_ready_gets_one_correction_after_same_sha_rerun(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot())
        ready = self.db.claim()
        self.assertTrue(self.db.begin(ready))
        self.db.finish(ready, "sent", receipt="receipt")
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, self.db.revision(REPO, 4), snapshot(check=None))
        self.db.apply(REPO, 4, self.db.revision(REPO, 4), snapshot(check=None))
        self.assertEqual("correction", self.db.claim()["kind"])

    def test_lost_close_still_direct_reads_old_unresolved_pr_on_restart(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot(review=None))
        self.now += 90 * 86400
        reopened = self.ledger.Ledger(self.path, lambda: self.now)
        provider = mock.Mock()
        provider.snapshot.return_value = snapshot(merged=True, review=None)
        self.module.reconcile(reopened, provider, full=True)
        provider.snapshot.assert_called_once_with(REPO, 4)
        self.assertEqual({"pending": 2}, reopened.status()["actions"])
        self.assertEqual("pr_merged", reopened.claim()["kind"])

    def test_source_ack_failure_recovers_without_duplicate_delivery(self):
        source = mock.Mock()
        source.events.return_value = [{"id": "1" * 32, "card": "a1", "at": self.now}]
        source.ack.side_effect = OSError
        with self.assertRaises(OSError):
            self.module.replay_board(self.db, source)
        source.ack.side_effect = None
        self.module.replay_board(self.ledger.Ledger(self.path, lambda: self.now), source)
        transport = mock.Mock(deliver=mock.Mock(return_value="fixture-receipt"))
        self.now += 4
        self.assertTrue(self.module.dispatch_one(self.db, mock.Mock(), transport, lane="todo"))
        self.assertFalse(self.module.dispatch_one(self.db, mock.Mock(), transport, lane="todo"))
        transport.deliver.assert_called_once()
        self.assertEqual(4, self.db.status()["max_first_attempt_seconds"])

    def test_process_kill_rolls_back_action_insert_and_cursor_together(self):
        event = {"id": "1" * 32, "card": "a1", "at": self.now}
        code = """
import json, os, signal, sys
from supervisor_ledger import Ledger
db = Ledger(sys.argv[1])
phase = sys.argv[2]
event = json.loads(sys.argv[3])
if phase == "insert":
    action = db._action
    def interrupt(*args):
        action(*args)
        os.kill(os.getpid(), signal.SIGKILL)
    db._action = interrupt
db.ingest_board([event])
os.kill(os.getpid(), signal.SIGKILL)
"""
        for phase, expected in (("insert", {}), ("commit", {"pending": 1})):
            child = subprocess.run(
                [sys.executable, "-B", "-c", code, str(self.path), phase, json.dumps(event)],
                env={**os.environ, "PYTHONPATH": str(PLUGIN)},
                timeout=10,
                capture_output=True,
            )
            self.assertEqual(-signal.SIGKILL, child.returncode, child.stderr)
            self.assertEqual(expected, self.db.status()["actions"])
            self.assertEqual(event["id"] if expected else None, self.db.status()["journal_cursor"])
        self.db.ingest_board([event])
        self.assertEqual({"pending": 1}, self.db.status()["actions"])

    def test_process_kill_after_dispatch_fence_preserves_unknown(self):
        event = {"id": "1" * 32, "card": "a1", "at": self.now}
        self.db.ingest_board([event])
        child = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                """
import os, signal, sys
from supervisor_ledger import Ledger
db = Ledger(sys.argv[1], lambda: 100)
assert db.begin(db.claim())
os.kill(os.getpid(), signal.SIGKILL)
""",
                str(self.path),
            ],
            env={**os.environ, "PYTHONPATH": str(PLUGIN)},
            timeout=10,
            capture_output=True,
        )
        self.assertEqual(-signal.SIGKILL, child.returncode, child.stderr)
        self.now += 121
        self.assertIsNone(self.db.claim())
        self.assertEqual({"unknown": 1}, self.db.status()["actions"])

    def test_dispatch_reads_new_head_dismissal_and_same_head_retry(self):
        for index, replacement in enumerate(
            (snapshot(NEXT, review=None), snapshot(review=None), snapshot(check=None)), 1
        ):
            self.db.track(REPO, index)
            self.db.apply(REPO, index, 1, snapshot())
            provider, transport = mock.Mock(), mock.Mock(deliver=mock.Mock(return_value="fixture-receipt"))
            provider.snapshot.return_value = replacement
            self.assertTrue(self.module.dispatch_one(self.db, provider, transport))
            transport.deliver.assert_not_called()
        self.assertEqual({"cancelled": 3}, self.db.status()["actions"])

    def test_dismissal_lost_beyond_overlap_corrects_sent_snapshot(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot())
        provider, transport = mock.Mock(), mock.Mock(deliver=mock.Mock(return_value="fixture-receipt"))
        provider.snapshot.return_value = snapshot()
        self.module.dispatch_one(self.db, provider, transport)
        self.now += 90 * 86400
        provider.snapshot.return_value = snapshot(review=None)
        self.module.reconcile(self.db, provider, full=True)
        self.module.dispatch_one(self.db, provider, transport)
        self.assertEqual(["ready", "correction"], [args[0] for args, _ in transport.deliver.call_args_list])
        ready, correction = [args for args, _ in transport.deliver.call_args_list]
        self.assertTrue(ready[2].endswith("+00:00"))
        self.assertEqual(self.now, correction[1]["superseded_at"])

    def test_quota_one_request_and_one_owner_fallback_per_head(self):
        self.db.track(REPO, 4)
        state = snapshot(review=None)
        self.db.apply(REPO, 4, 1, state, request_copilot=True)
        provider, transport = mock.Mock(), mock.Mock(deliver=mock.Mock(return_value="fixture-receipt"))
        provider.snapshot.return_value = state
        provider.request_review.side_effect = self.module.ProviderFailure("copilot_quota")
        for _ in range(2):
            self.module.dispatch_one(self.db, provider, transport, request_copilot=True)
        provider.request_review.assert_called_once_with(REPO, 4)
        transport.deliver.assert_called_once()
        self.assertEqual("blocked_notice", transport.deliver.call_args.args[0])
        self.assertEqual("copilot_quota", self.db.status()["attention"][0]["reason"])
        self.module.reconcile(self.db, provider, full=True, request_copilot=True)
        self.assertFalse(self.module.dispatch_one(self.db, provider, transport, request_copilot=True))

    def test_closed_without_merge_and_outdated_feedback_do_not_notify(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot(review=None, threads=[123]))
        provider, transport = mock.Mock(), mock.Mock(deliver=mock.Mock(return_value="fixture-receipt"))
        provider.snapshot.return_value = snapshot(closed=True, review=None)
        self.module.dispatch_one(self.db, provider, transport)
        transport.deliver.assert_not_called()
        self.assertEqual({"cancelled": 1}, self.db.status()["actions"])

    def test_check_requires_current_attempt_on_exact_sha(self):
        provider = self.adapters.GitHub("fixture", {"test"}, set())
        check = {
            "id": 81,
            "name": "test",
            "app": {"slug": "github-actions"},
            "conclusion": "success",
            "details_url": f"https://github.com/{REPO}/actions/runs/22/job/55",
        }
        pr = {"number": 4, "base": {"ref": "main", "repo": {"full_name": REPO}, "sha": BASE}}
        current = {
            "pull_requests": [{"number": 4, "base": pr["base"], "head": {"sha": HEAD}}],
            "head_sha": HEAD,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
        }
        job = {"check_run_url": "https://api.github.com/check-runs/81", "conclusion": "success"}
        for attempt_jobs, status, expected in (
            ([job], "completed", [["test", 81, 22, 2]]),
            ([], "completed", []),
            ([job], "queued", []),
        ):
            with (
                mock.patch.object(provider, "get", return_value={**current, "status": status}),
                mock.patch.object(provider, "pages", side_effect=[[check], attempt_jobs]),
            ):
                self.assertEqual(expected, provider.checks(REPO, pr, HEAD, BASE))

    def test_owner_route_uses_current_assignment_and_ignores_untrusted_text(self):
        board = mock.Mock()
        url = f"https://github.com/{REPO}/pull/4"
        delivery = self.adapters.Transport(board, "cht_fixture", "secret_canary", "node/main:Boss", ["mp"], 5)
        data = {"repo": REPO, "number": 4, "head": HEAD, "thread": 123}
        board.read.return_value = {
            "tasks": {
                "old": {"assignee": "node/main:old", "state": "done", "text": url},
                "new": {
                    "assignee": "node/main:new",
                    "state": "working",
                    "text": url + " private_canary",
                    "supervisorPRs": [url],
                },
                "other": {"assignee": "node/main:other", "state": "working", "text": url + "0"},
            }
        }
        with mock.patch.object(self.adapters.subprocess, "run") as send:
            send.return_value.returncode = 0
            self.assertEqual("agent_transport_accepted", delivery.deliver("review", data, None))
            self.assertEqual("node/main:new", send.call_args.args[0][2])
            self.assertNotIn("private_canary", send.call_args.args[0][3])
            self.assertNotIn("secret_canary", send.call_args.args[0][3])
            send.side_effect = [mock.Mock(returncode=3), mock.Mock(returncode=0)]
            delivery.deliver("review", data, None)
            self.assertEqual("node/main:Boss", send.call_args.args[0][2])

    def test_throttle_retries_after_deadline_and_ambiguous_never_replays(self):
        self.db.ingest_board([{"id": "1" * 32, "card": "a1", "at": self.now}])
        provider = mock.Mock()
        transport = mock.Mock()
        transport.deliver.side_effect = [
            self.module.ProviderFailure("rate_limited", delay=12),
            self.module.ProviderFailure("provider_unconfirmed", unknown=True),
        ]
        self.module.dispatch_one(self.db, provider, transport)
        self.now += 11
        self.assertFalse(self.module.dispatch_one(self.db, provider, transport))
        self.now += 2
        self.module.dispatch_one(self.db, provider, transport)
        self.now += 600
        self.assertFalse(self.module.dispatch_one(self.db, provider, transport))
        self.assertEqual({"unknown": 1}, self.db.status()["actions"])
        self.assertEqual(2, transport.deliver.call_count)

    def test_webhook_updates_arriving_out_of_order_cannot_change_merge_fact(self):
        provider, transport = mock.Mock(), mock.Mock()
        provider.snapshot.return_value = snapshot(merged=True)
        transport.deliver.return_value = "fixture-receipt"
        for index, action in enumerate(("closed", "synchronize", "closed"), 1):
            raw = json.dumps({"action": action, "repository": {"full_name": REPO}, "number": 4}).encode()
            sig = "sha256=" + hmac.new(b"fixture", raw, hashlib.sha256).hexdigest()
            self.module.ingest_webhook(self.db, {REPO}, b"fixture", sig, "pull_request", f"d{index}", raw)
            self.module.reconcile(self.db, provider)
            while self.module.dispatch_one(self.db, provider, transport):
                pass
        self.assertEqual(["pr_merged", "pr_merged_owner"], [args[0] for args, _ in transport.deliver.call_args_list])

    def test_same_head_other_pr_or_target_cannot_supply_check(self):
        provider = self.adapters.GitHub("fixture", {"test"}, set())
        pr = {"number": 4, "base": {"ref": "main", "repo": {"full_name": REPO}, "sha": BASE}}
        check = {
            "id": 81,
            "name": "test",
            "app": {"slug": "github-actions"},
            "conclusion": "success",
            "details_url": f"https://github.com/{REPO}/actions/runs/22/job/55",
        }
        job = {"check_run_url": "https://api.github.com/check-runs/81", "conclusion": "success"}
        for number, ref, expected in ((5, "main", []), (4, "other", []), (4, "main", [["test", 81, 22, 2]])):
            with self.subTest(number=number, ref=ref):
                run = {
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 2,
                    "pull_requests": [{"number": number, "base": {**pr["base"], "ref": ref}, "head": {"sha": HEAD}}],
                }
                with (
                    mock.patch.object(provider, "get", return_value=run),
                    mock.patch.object(provider, "pages", side_effect=[[check], [job]]),
                ):
                    self.assertEqual(expected, provider.checks(REPO, pr, HEAD, BASE))

    def test_synthetic_sha_rejects_conflicting_pr_and_target_despite_same_parents(self):
        provider = self.adapters.GitHub("fixture", {"test"}, set())
        target = {"ref": "main", "repo": {"id": 123}, "sha": BASE}
        pr = {"number": 4, "base": target, "merge_commit_sha": MERGE}
        link = {"number": 4, "base": target, "head": {"sha": HEAD}}
        check = {
            "id": 81,
            "name": "test",
            "app": {"slug": "github-actions"},
            "conclusion": "success",
            "details_url": f"https://github.com/{REPO}/actions/runs/22/job/55",
        }
        job = {"check_run_url": "https://api.github.com/check-runs/81", "conclusion": "success"}
        for associated, expected in (
            ([], [["test", 81, 22, 2]]),
            ([link], [["test", 81, 22, 2]]),
            ([{**link, "number": 5}], []),
            ([{**link, "base": {**target, "ref": "release"}}], []),
            ([link, {**link, "number": 5}], []),
        ):
            with self.subTest(associated=associated):
                run = {
                    "head_sha": MERGE,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 2,
                    "pull_requests": associated,
                }
                with (
                    mock.patch.object(provider, "get", side_effect=[{"parents": [{"sha": BASE}, {"sha": HEAD}]}, run]),
                    mock.patch.object(provider, "pages", side_effect=[[], [check], [job]]),
                ):
                    self.assertEqual(expected, provider.checks(REPO, pr, HEAD, BASE))

    def test_historical_bot_review_reconciles_old_unknown_and_unblocks_only_new_head(self):
        self.db.track(REPO, 4)
        self.db.apply(REPO, 4, 1, snapshot(review=None), request_copilot=True)
        action = self.db.claim()
        self.assertTrue(self.db.begin(action))
        self.db.finish(action, "unknown", reason="copilot_request_head_unconfirmed")
        self.now += 100
        provider = self.adapters.GitHub("fixture", set(), set())
        pr = {"head": {"sha": NEXT}, "base": {"sha": BASE}, "state": "open", "draft": False}
        old = {
            "id": 81,
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "commit_id": HEAD,
            "state": "COMMENTED",
            "submitted_at": "1970-01-01T00:01:39Z",
        }
        timely = {**old, "id": 82, "submitted_at": "1970-01-01T00:01:41Z"}
        unrelated = {**timely, "commit_id": MERGE, "id": 83}
        for rows in ([old, unrelated], [old, timely, unrelated]):
            with (
                mock.patch.object(provider, "get", return_value=pr),
                mock.patch.object(provider, "pages", return_value=rows),
                mock.patch.object(provider, "threads", return_value=[]),
            ):
                self.module.reconcile(self.db, provider, full=True, request_copilot=True)
            if len(rows) == 2:
                self.assertIsNone(self.db.claim())
        updated = self.db.claim()
        self.assertIsNotNone(updated)
        self.assertEqual("copilot_request", updated["kind"])
        self.assertEqual(NEXT, updated["data"]["head"])
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status,receipt FROM actions WHERE id=?", (action["id"],)).fetchone()
        self.assertEqual(("sent", "exact_review_observed:82"), tuple(row))

    def test_copilot_post_and_conflict_require_exact_head_review_readback(self):
        for number, (result, after, expected) in enumerate(
            (
                (
                    "request_accepted",
                    {**snapshot(NEXT, review=None), "copilot": "reviewed", "copilot_review": 75},
                    "unknown",
                ),
                ("already_pending", {**snapshot(review=None), "copilot": "pending"}, "unknown"),
                ("already_pending", {**snapshot(review=None), "copilot": "reviewed", "copilot_review": 76}, "sent"),
            ),
            1,
        ):
            with self.subTest(result=result, head=after["head"], expected=expected):
                self.db.track(REPO, number)
                before = snapshot(review=None)
                self.db.apply(REPO, number, 1, before, request_copilot=True)
                github = self.adapters.GitHub("fixture", set(), set())
                order = []

                def read(_repo, _number):
                    order.append("read")
                    return before if len(order) == 1 else after

                def post(url, headers, data, **kwargs):
                    order.append("post")
                    self.assertTrue(url.endswith(f"/pulls/{number}/copilot-code-review"))
                    self.assertEqual({}, data)
                    if result == "already_pending":
                        raise urllib.error.HTTPError(url, 409, "conflict", {}, io.BytesIO(b"{}"))
                    response = mock.MagicMock()
                    response.__enter__.return_value.read.return_value = b'{"state":"PENDING"}'
                    return response

                with (
                    mock.patch.object(github, "snapshot", side_effect=read),
                    mock.patch.object(
                        self.adapters.urllib.request.OpenerDirector,
                        "open",
                        side_effect=lambda request, **opts: post(
                            request.full_url, request.headers, json.loads(request.data), **opts
                        ),
                    ),
                ):
                    self.assertTrue(self.module.dispatch_one(self.db, github, mock.Mock(), request_copilot=True))
                    self.assertEqual(["read", "post", "read"], order)
                    self.now += 1000
                    self.assertFalse(self.module.dispatch_one(self.db, github, mock.Mock(), request_copilot=True))
                    if expected == "unknown":
                        # A later empty snapshot cannot trigger another POST on the old or new SHA.
                        self.db.apply(
                            REPO,
                            number,
                            self.db.revision(REPO, number),
                            snapshot(NEXT, review=None),
                            request_copilot=True,
                        )
                        self.assertFalse(self.module.dispatch_one(self.db, github, mock.Mock(), request_copilot=True))
                        self.assertEqual(["read", "post", "read"], order)
                with self.db.transaction() as conn:
                    row = conn.execute(
                        "SELECT status,receipt FROM actions WHERE id=?", (f"copilot:{REPO}#{number}:{HEAD}",)
                    ).fetchone()
                self.assertEqual(expected, row["status"])
                self.assertEqual("already_pending:review:76" if expected == "sent" else None, row["receipt"])

    def test_injected_plain_pr_links_cannot_override_structured_owner(self):
        board = mock.Mock()
        url = f"https://github.com/{REPO}/pull/4"
        delivery = self.adapters.Transport(board, "cht_fixture", "token", "node/main:Boss", ["mp"], 5)
        board.read.return_value = {
            "tasks": {
                "attacker": {
                    "assignee": "node/main:attacker",
                    "state": "working",
                    "text": url,
                    "comments": [{"text": url}],
                    "proofs": [{"url": url}],
                },
                "owner": {"assignee": "node/main:owner", "state": "working", "supervisorPRs": [url]},
            }
        }
        data = {"repo": REPO, "number": 4, "head": HEAD, "thread": 123}
        with mock.patch.object(self.adapters.subprocess, "run", return_value=mock.Mock(returncode=0)) as send:
            delivery.deliver("review", data, None)
            self.assertEqual("node/main:owner", send.call_args.args[0][2])
            board.read.return_value["tasks"].pop("owner")
            delivery.deliver("review", data, None)
            self.assertEqual("node/main:Boss", send.call_args.args[0][2])

    def test_discovery_reports_gap_and_retains_old_open_pr_before_restart(self):
        provider = mock.Mock()
        provider.discover.return_value = iter([{"updated_at": "1970-01-01T00:00:00Z", "state": "open", "number": 4}])
        self.service.discover(self.db, provider, {REPO})
        self.assertEqual([(REPO, 4, 1)], self.db.tracked())
        self.assertEqual("history_before_enrollment_uncovered", self.db.status()["gaps"][0]["reason"])
        self.now += 90 * 86400
        provider.discover.side_effect = self.module.ProviderFailure("pagination_coverage_gap")
        self.service.discover(self.db, provider, {REPO})
        self.assertEqual({f"history:{REPO}", f"discovery:{REPO}"}, {gap["scope"] for gap in self.db.status()["gaps"]})


class BoardJournalTests(unittest.TestCase):
    def test_anonymous_todo_page_does_not_mint_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = sandbox.load(tmp, "todo-server.py")
            with (
                mock.patch.object(srv, "supervisor_journal_key", return_value="fixture"),
                http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler) as server,
            ):
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    client = http.client.HTTPConnection(*server.server_address, timeout=3)
                    try:
                        client.request("GET", "/todos")
                        response = client.getresponse()
                        self.assertEqual((401, False), (response.status, bool(response.getheader("Set-Cookie"))))
                        response.read()
                    finally:
                        client.close()
                finally:
                    server.shutdown()
                    thread.join(3)

    def test_anonymous_or_machine_cannot_write_card_without_owner_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "credential"
            owner_key, journal_key = "o" * 36, "j" * 36
            key.write_text(hashlib.sha256(owner_key.encode()).hexdigest(), encoding="ascii")
            key.chmod(0o600)
            srv = sandbox.load(tmp, "todo-server.py", MYPEOPLE_BOARD_BACKEND="json")
            self.assertTrue(
                srv.save_board({"version": 2, "order": ["a1"], "tasks": {"a1": {"id": "a1", "state": "working"}}})
            )
            with (
                mock.patch.dict(os.environ, {"SUPERVISOR_OWNER_VERIFIER_FILE": str(key)}),
                mock.patch.object(srv, "supervisor_journal_key", return_value=journal_key),
                mock.patch.object(srv, "ping_boss"),
                mock.patch.object(srv, "request_owner_lifecycle"),
                http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler) as server,
            ):
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                self.addCleanup(thread.join, 3)
                try:

                    def call(method, path, body=None, headers=None):
                        client = http.client.HTTPConnection(*server.server_address, timeout=3)
                        try:
                            client.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                            response = client.getresponse()
                            cookie = response.getheader("Set-Cookie")
                            response.read()
                            return response.status, cookie.split(";", 1)[0] if cookie else None
                        finally:
                            client.close()

                    _, anonymous = call("GET", "/plugins")
                    self.assertIsNotNone(anonymous)
                    basic = "Basic " + base64.b64encode(f"owner:{owner_key}".encode()).decode()
                    status, authenticated = call("GET", "/todos", headers={"Authorization": basic})
                    self.assertEqual(200, status)
                    self.assertIsNotNone(authenticated)
                    fake_session = {"Cookie": anonymous}
                    machine = {"X-Queue-Secret": "secret"}
                    owner = {"Cookie": authenticated, "Authorization": basic}
                    url = f"https://github.com/{REPO}/pull/4"
                    close = {"task_id": "a1", "state": "done", "by": "CEO"}
                    link = {"id": "a1", "op": "set", "supervisorPRs": [url]}
                    for path, body in (("/todo/status", close), ("/todo/update", link)):
                        self.assertEqual(401, call("POST", path, body)[0])
                        self.assertEqual(403, call("POST", path, body, fake_session)[0])
                        self.assertEqual(403, call("POST", path, body, machine)[0])
                    untouched = srv._load_disk()
                    self.assertEqual("working", untouched["tasks"]["a1"]["state"])
                    self.assertNotIn("supervisorPRs", untouched["tasks"]["a1"])
                    self.assertNotIn("supervisorJournal", untouched)
                    self.assertEqual(200, call("POST", "/todo/status", close, owner)[0])
                    self.assertEqual(200, call("POST", "/todo/update", link, owner)[0])
                    change = {**link, "supervisorPRs": [url + "1"]}
                    self.assertEqual(403, call("POST", "/todo/update", change, fake_session)[0])
                    self.assertEqual(403, call("POST", "/todo/update", change, machine)[0])
                    self.assertEqual([url], srv._load_disk()["tasks"]["a1"]["supervisorPRs"])
                    self.assertEqual(1, len(srv._load_disk()["supervisorJournal"]))
                finally:
                    server.shutdown()

    def test_process_kill_around_source_commit_keeps_card_and_event_atomic(self):
        code = """
import os, signal, sys
from unittest import mock
import sandbox
mod = sandbox.load(sys.argv[1], "todo-server.py", MYPEOPLE_BOARD_BACKEND=sys.argv[2])
fixture = {"version": 2, "order": ["a1"], "tasks": {"a1": {"id": "a1", "state": "working"}}}
assert mod.save_board(fixture)
original = mod.save_board
def save(*args, **kwargs):
    if sys.argv[3] == "after":
        assert original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
with mock.patch.object(mod, "save_board", save), mock.patch.object(mod, "supervisor_journal_key", return_value="fixture"), mock.patch.object(mod, "supervisor_owner_authorized", return_value=True):
    mod.Handler._status_op(mock.Mock(), {"task_id": "a1", "state": "done"}, "browser")
"""
        for backend in ("json", "sqlite"):
            for phase in ("before", "after"):
                with self.subTest(backend=backend, phase=phase), tempfile.TemporaryDirectory() as tmp:
                    child = subprocess.run(
                        [sys.executable, "-B", "-c", code, tmp, backend, phase],
                        cwd=Path(__file__).resolve().parents[1],
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(-signal.SIGKILL, child.returncode, child.stderr)
                    board = sandbox.load(tmp, "todo-server.py", MYPEOPLE_BOARD_BACKEND=backend)._load_disk()
                    self.assertEqual("done" if phase == "after" else "working", board["tasks"]["a1"]["state"])
                    self.assertEqual(int(phase == "after"), len(board.get("supervisorJournal", [])))

    def test_done_journal_requires_owner_session_and_successful_commit(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                mod = sandbox.load(tmp, "todo-server.py", MYPEOPLE_BOARD_BACKEND=backend)
                fixture = {
                    "version": 2,
                    "order": ["a1"],
                    "pinSeq": 0,
                    "tasks": {"a1": {"id": "a1", "state": "working"}},
                }
                self.assertTrue(mod.save_board(fixture))
                handler = mock.Mock()
                handler._send.side_effect = lambda status, body: (status, body)
                with (
                    mock.patch.object(mod, "supervisor_journal_key", return_value="fixture"),
                    mock.patch.object(mod, "supervisor_owner_authorized", return_value=True),
                    mock.patch.object(mod, "ping_boss"),
                    mock.patch.object(mod, "request_owner_lifecycle") as lifecycle,
                ):
                    data = {"task_id": "a1", "state": "done", "by": "CEO"}
                    self.assertEqual(403, mod.Handler._status_op(handler, data, "machine")[0])
                    with mock.patch.object(mod, "save_board", return_value=False):
                        self.assertEqual(409, mod.Handler._status_op(handler, data, "browser")[0])
                    lifecycle.assert_not_called()
                    self.assertEqual(200, mod.Handler._status_op(handler, data, "browser")[0])
                    persisted = mod._load_disk()
                    self.assertEqual("done", persisted["tasks"]["a1"]["state"])
                    journal = persisted["supervisorJournal"]
                    self.assertEqual(["a1"], [event["card"] for event in journal])
                    self.assertNotIn("supervisorJournal", json.loads(mod.board_json()))
                    self.assertEqual(200, mod.Handler._status_op(handler, data, "browser")[0])
                    self.assertEqual(journal, mod._load_disk()["supervisorJournal"])
                    with mock.patch.object(mod, "save_board", return_value=False):
                        self.assertEqual(409, mod.supervisor_ack([journal[0]["id"]])[0])
                    self.assertEqual(journal, mod.supervisor_events())
                    self.assertEqual(200, mod.supervisor_ack([journal[0]["id"]])[0])
                    self.assertEqual([], mod._load_disk()["supervisorJournal"])
                    mod.Handler._status_op(handler, {"task_id": "a1", "state": "working"}, "browser")
                    mod.Handler._status_op(handler, data, "browser")
                    next_event = mod._load_disk()["supervisorJournal"][0]
                    self.assertNotEqual(journal[0]["id"], next_event["id"])
