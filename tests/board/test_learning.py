import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

import sandbox


class LearningHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "state/state/learning/episodes.sqlite3"
        self.grants = self.root / "grants.json"
        rows = {}
        self.tokens = {}
        for index, (name, origin, scope) in enumerate(
            (
                ("owner", "owner", "o/p"),
                ("evaluator", "evaluator", "o/p"),
                ("writer", "worker", "o/p"),
                ("reader", "worker", "o/p"),
                ("other", "worker", "o/other"),
                ("other_owner", "owner", "o/other"),
                ("other_evaluator", "evaluator", "o/other"),
                ("provider", "provider", "o/p"),
            ),
            1,
        ):
            token = f"{index:064x}"
            self.tokens[name] = self.root / name
            self.tokens[name].write_text(token)
            self.tokens[name].chmod(0o600)
            rows[hashlib.sha256(token.encode()).hexdigest()] = {"origin": origin, "scopes": [[scope, "engineer"]]}
        self.grants.write_text(json.dumps(rows))
        self.grants.chmod(0o600)
        self.board = sandbox.load(str(self.root), "todo-server.py", LEARNING_GRANTS_FILE=str(self.grants))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.board.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()

    def call(self, actor, op, data, *, repo="o/p", role="engineer", rc=0):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "TODO_PORT": str(self.server.server_port),
            "MYPEOPLE_CONFIG_PATH": str(self.root / "queue.env"),
            "QUEUE_SECRET": "secret",
            "MYPEOPLE_LEARNING_TOKEN_FILE": str(self.tokens[actor]),
        }
        proc = subprocess.run(
            [sys.executable, "-B", str(sandbox.BIN / "mp"), "learning"],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            input=json.dumps({"op": op, "repo": repo, "role": role, "data": data}),
        )
        self.assertEqual(rc, proc.returncode, proc.stderr)
        return json.loads(proc.stdout) if rc == 0 else proc.stderr

    def record(self, incident="1" * 32, actor="owner", **scope):
        return self.call(
            actor,
            "record",
            {
                "incident": incident,
                "event": "periodic",
                "observed": "closed",
                "decision": "silence",
                "outcome": "confirmed",
                "evidence": "a" * 32,
            },
            **scope,
        )

    def proposal(self, lesson="2" * 32, incident="1" * 32, **updates):
        return {
            "lesson": lesson,
            "incident": incident,
            "event": "periodic",
            "condition": "closed",
            "action": "silence",
            "exceptions": ["unknown"],
            "valid_from": 0,
            "expires": 4_000_000_000,
            **updates,
        }

    def propose(self, actor="owner", repo="o/p", **updates):
        data = self.proposal(**updates)
        data["expires"] = int(self.board.time.time()) + 3600
        return self.call(actor, "propose", data, repo=repo)

    def transition(self, lesson, actor, state, *, rc=0, **scope):
        return self.call(
            actor,
            "transition",
            {"lesson": lesson["lesson"], "version": lesson["version"], "digest": lesson["digest"], "state": state},
            rc=rc,
            **scope,
        )

    def activate(self, lesson):
        self.transition(lesson, "evaluator", "evaluated")
        self.transition(lesson, "owner", "shadow")
        self.transition(lesson, "owner", "active")

    def lookup(self, actor="reader", *, rc=0, exceptions=(), **kwargs):
        return self.call(
            actor,
            "lookup",
            {"event": "periodic", "observed": "closed", "exceptions": list(exceptions)},
            rc=rc,
            **kwargs,
        )

    def test_two_workers_share_only_active_scoped_lesson_and_revocation(self):
        self.assertFalse(self.record()["duplicate"])
        self.assertTrue(self.record()["duplicate"])
        draft = self.propose("writer")
        self.assertEqual("no_match", self.lookup()["reason"])
        self.assertIn("403", self.transition(draft, "writer", "active", rc=1))
        self.assertIn("403", self.transition(draft, "owner", "evaluated", rc=1))
        self.activate(draft)
        self.assertEqual("silence", self.lookup()["action"])
        self.assertEqual(draft["digest"], self.lookup()["consulted"][0]["digest"])
        self.assertEqual("no_match", self.lookup(exceptions=["unknown"])["reason"])
        self.assertIn("403", self.lookup("other", rc=1))
        self.assertIn("403", self.lookup(role="boss", rc=1))
        self.assertEqual("no_match", self.lookup("other", repo="o/other")["reason"])
        second = self.propose(action="route_worker")
        self.assertEqual(2, second["version"])
        self.activate(second)
        self.assertEqual("route_worker", self.lookup()["action"])
        self.transition(second, "owner", "revoked")
        self.assertEqual("no_match", self.lookup()["reason"])

    def test_scope_authentication_expiry_and_origin_removal(self):
        self.assertIn("400", self.call("writer", "record", {"raw_message": "private_canary"}, rc=1))
        self.assertIn("403", self.call("provider", "propose", self.proposal(), rc=1))
        self.record()
        first = self.propose()
        self.activate(first)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE lessons SET expires=0")
        self.assertEqual("no_match", self.lookup()["reason"])
        replacement = self.propose()
        self.activate(replacement)
        self.assertTrue(self.call("owner", "erase", {"incident": "1" * 32})["erased"])
        self.assertEqual("no_match", self.lookup()["reason"])
        with sqlite3.connect(self.db) as conn:
            for table in ("episodes", "lessons", "transitions", "lineages"):
                self.assertEqual(0, conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        self.record()
        third = self.propose()
        self.assertEqual(1, third["version"])
        self.assertNotEqual(replacement["digest"], third["digest"])
        self.assertIn("403", self.transition(replacement, "evaluator", "evaluated", rc=1))
        self.tokens["reader"].chmod(0o644)
        self.assertIn("credential", self.lookup(rc=2))

    def test_explicit_correction_wins_and_equal_authority_conflict_abstains(self):
        self.record()
        first = self.propose("writer")
        self.activate(first)
        other = self.propose("writer", lesson="3" * 32, action="notify_owner")
        self.activate(other)
        self.assertEqual("conflict", self.lookup()["reason"])
        explicit = self.propose(lesson="4" * 32, action="route_worker")
        self.activate(explicit)
        self.assertEqual("route_worker", self.lookup()["action"])

    def test_equal_ids_across_scopes_and_episode_expiry(self):
        self.record()
        self.record(actor="other_owner", repo="o/other")
        first = self.propose()
        second = self.propose("other_owner", repo="o/other", action="route_worker")
        self.assertEqual((1, 1), (first["version"], second["version"]))
        self.activate(first)
        for actor, state in (("other_evaluator", "evaluated"), ("other_owner", "shadow"), ("other_owner", "active")):
            self.transition(second, actor, state, repo="o/other")
        self.assertEqual("silence", self.lookup()["action"])
        self.assertEqual("route_worker", self.lookup("other", repo="o/other")["action"])
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE episodes SET expires=0 WHERE repo='o/p'")
        self.assertEqual("route_worker", self.lookup("other", repo="o/other")["action"])
        with sqlite3.connect(self.db) as conn:
            for table in ("episodes", "lessons", "transitions", "lineages"):
                self.assertEqual(0, conn.execute(f"SELECT count(*) FROM {table} WHERE repo='o/p'").fetchone()[0])
