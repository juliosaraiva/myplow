"""The shipped client, broker and Boss commands against disposable local identities."""

import http.client
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

import sandbox


PLUGINS = Path(__file__).resolve().parents[2] / "mypeople/runtime/plugins"
ENTRY = PLUGINS / "private-route/private-route.py"
TASK_TEXT = "Synthetic task, private route test only"
APPROVAL_TEXT = "Synthetic approval question"
RESULT_TEXT = "Synthetic private result"


class PrivateRouteTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="mp-route-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.endpoint = self.state / "route.sock"
        self.credential = self.root / "agent.cred"
        self.other = self.root / "other.cred"
        self.audit = self.root / "prohibited-io.log"
        self.daemon_logs = self.root / "broker.log"
        self.human_events = []
        self.herdr_binary = os.environ.get("PRIVATE_ROUTE_TEST_HERDR")
        self.herdr = None
        for kind in ("home", "config", "runtime"):
            (self.root / kind).mkdir(mode=0o700)
        self.env = {
            "HOME": str(self.root / "home"),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "HC_RUNTIME": str(self.root / "runtime"),
            "HC_SOCKET": str(self.root / "core.sock"),
            "PYTHONPATH": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        (self.root / "sitecustomize.py").write_text(
            "import os, socket, sys\n"
            "def audit(event, args):\n"
            "    if event == 'socket.__new__' and args[1] != socket.AF_UNIX:\n"
            "        sink = 'network'\n"
            f"    elif event == 'socket.connect' and args[1] != {str(self.endpoint)!r}:\n"
            "        sink = 'other_socket'\n"
            "    elif event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'):\n"
            "        sink = 'process'\n"
            "    else:\n"
            "        return\n"
            f"    with open({str(self.audit)!r}, 'a') as counter:\n"
            "        counter.write(sink + '\\n')\n"
            "    raise PermissionError('fixture prohibited I/O')\n"
            "sys.addaudithook(audit)\n",
            encoding="utf-8",
        )
        self.run_cli("route", "on")
        self.run_cli("grant", "xurupito", "--allow-task", "--credential-file", self.credential)
        self.run_cli("grant", "reader", "--credential-file", self.other)
        self.addCleanup(self.stop_service)
        self.start_service()

    def cli(self, *args):
        return [sys.executable, str(ENTRY), "--state-dir", str(self.state), *map(str, args)]

    def invoke(self, argv, note=None):
        return subprocess.run(argv, input=note, capture_output=True, text=True, env=self.env, timeout=12)

    def run_cli(self, *args, note=None, error=None):
        argv = self.cli(*args)
        if self.herdr and args[0] == "boss":
            argv = [self.herdr, "exec", "-i", "--", *argv]
        proc = self.invoke(argv, note or "")
        if error:
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout or proc.stderr), {"error": error})
            return {}
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout else {}

    def client(self, *args, credential=None, note=None, error=None):
        return self.run_cli("client", "--credential-file", credential or self.credential, *args, note=note, error=error)

    def start_service(self):
        with self.daemon_logs.open("ab") as output:
            self.service = subprocess.Popen(self.cli("serve"), env=self.env, stdout=output, stderr=output)
        for _ in range(200):
            if self.service.poll() is not None:
                self.fail("isolated broker exited during startup")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(self.endpoint))
                    return
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.01)
        self.fail("isolated socket unavailable")

    def stop_service(self, crash=False):
        if self.service.poll() is None:
            if crash:
                self.service.kill()
            else:
                self.service.terminate()
            self.service.wait(timeout=5)

    def post(self, path, body, credential=None, raw=None):
        connection = http.client.HTTPConnection("localhost", timeout=3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(self.endpoint))
            connection.sock = sock
            headers = {"Content-Type": "application/json"}
            if credential:
                headers["Authorization"] = "Bearer " + credential
            connection.request("POST", path, body=json.dumps(body) if raw is None else raw, headers=headers)
            result = connection.getresponse()
            return result.status, json.loads(result.read())

    def positive_human_control(self):
        human = self.root / "human"
        human.mkdir(mode=0o700)
        env = {**self.env, **sandbox.PINNED, "INSTALL_DIR": str(self.root / "runtime"), "PLOW_CHAT_STATE_DIR": str(human)}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules), mock.patch.object(sys, "path", sys.path[:]):
            sys.modules.pop("mpcommon", None)
            plugin = sandbox.load_file(PLUGINS / "plow-chat/plow-chat.py", "private_route_human_fixture")
        chat = "cht_human_fixture"
        credentials = {"token": "fixture-only", "chat_uid": chat}
        answer = "Status for the prebound human fixture"
        inbound = []

        def local_api(method, path, body=None, token=None):
            if method == "GET" and path.startswith(f"/v1/chats/{chat}/messages?"):
                return 200, {"data": inbound}
            self.human_events.append((method, path, body))
            return 200, {"data": {}}

        def fixture_boss(message, agent=None, **kwargs):
            self.assertIn("human-control-input", message)
            plugin.send_message(answer, creds=credentials)
            return True

        with mock.patch.multiple(plugin, api=local_api, log=lambda *_: None, send_to_agent=fixture_boss):
            self.assertEqual(plugin.poll_chat(credentials, chat), 0)
            inbound.append({
                "uid": "control",
                "direction": "inbound",
                "body": "human-control-input",
                "chat_uid": chat,
                "created_at": "1",
                "sender": {"display_name": "Julio fixture", "relationship": "owner", "type": "human"},
            })
            self.assertEqual(plugin.poll_chat(credentials, chat), 1)
        self.assertEqual(self.human_events, [("POST", f"/v1/chats/{chat}/messages", {"body": answer})])
        self.assertEqual(self.client("events")["events"], [])

    def herdr_call(self, *args):
        proc = self.invoke([self.herdr, *args])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_lifecycle_recovers_after_restart(self):
        self.positive_human_control()
        before = None
        if self.herdr_binary:
            self.herdr = self.herdr_binary
            self.addCleanup(self.herdr_call, "server", "stop")
            before = self.herdr_call("server", "start")["pid"]
            self.assertEqual(self.herdr_call("ls", "--all")["terminals"], [])

        status = self.client("status", "unique-status")
        first = self.client("events")
        self.assertEqual(first["events"][0]["request_id"], status["request_id"])
        self.assertEqual(first["events"][0]["data"], {"route": "available"})
        cursor = first["next_cursor"]
        self.assertEqual(self.client("request", status["request_id"]), status)
        self.assertEqual(self.client("status", "unique-status"), status)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.client("task", "task-1", note=TASK_TEXT), range(2)))
        self.assertEqual(results[0], results[1])
        job = results[0]["request_id"]
        self.client("task", "task-1", note="different task", error="idempotency_conflict")
        self.client("approval", job, "approve", error="invalid_phase")
        inbox = self.run_cli("boss", "inbox")["requests"]
        self.assertEqual([(item["request_id"], item["profile"], item["task"]) for item in inbox], [(job, "xurupito", {"text": TASK_TEXT})])
        self.run_cli("boss", "accept", job)
        self.run_cli("boss", "accept", job)
        self.run_cli("boss", "request-approval", job, note=APPROVAL_TEXT)
        redirected = self.invoke(self.cli("boss", "result", job, "--destination", "human"), RESULT_TEXT)
        self.assertNotEqual(redirected.returncode, 0)
        self.run_cli("boss", "result", job, note=RESULT_TEXT, error="invalid_phase")
        self.client("approval", job, "approve", credential=self.other, error="not_found")
        approved = self.client("approval", job, "approve")
        self.assertEqual(self.client("approval", job, "approve"), approved)
        self.client("approval", job, "decline", error="idempotency_conflict")

        self.run_cli("route", "off")
        self.client("task", "failed-route", note=TASK_TEXT, error="route_unavailable")
        self.run_cli("boss", "result", job, note=RESULT_TEXT, error="route_unavailable")
        self.assertEqual(len(self.human_events), 1)
        self.stop_service(crash=True)
        self.assertTrue(self.endpoint.is_socket())
        if self.herdr:
            after = self.herdr_call("server", "restart")["pid"]
            self.assertNotEqual(before, after)
            self.assertEqual(self.herdr_call("ls", "--all")["terminals"], [])
        self.start_service()
        self.run_cli("route", "on")

        inbox = self.run_cli("boss", "inbox")["requests"]
        self.assertEqual([(item["request_id"], item["phase"]) for item in inbox], [(job, "approved")])
        completed = self.run_cli("boss", "result", job, note=RESULT_TEXT)
        self.assertEqual(self.run_cli("boss", "result", job, note=RESULT_TEXT), completed)
        self.run_cli("boss", "result", job, note="different result", error="idempotency_conflict")
        self.assertEqual(self.run_cli("boss", "inbox"), {"requests": []})
        page = self.client("events", "--cursor", cursor)
        self.assertEqual([item["type"] for item in page["events"]], ["task", "approval_required", "approval", "result"])
        self.assertEqual([item["data"] for item in page["events"]], [{}, {"text": APPROVAL_TEXT}, {"decision": "approve"}, {"text": RESULT_TEXT}])
        self.assertEqual({item["request_id"] for item in page["events"]}, {job})
        self.assertEqual(len({item["id"] for item in page["events"]}), 4)
        self.assertEqual(self.client("events", "--cursor", cursor), page)
        self.assertEqual(self.client("events", "--cursor", page["next_cursor"])["events"], [])
        self.assertEqual(self.client("events", credential=self.other)["events"], [])
        self.client("events", "--cursor", cursor, credential=self.other, error="not_found")
        self.client("request", job, credential=self.other, error="not_found")
        self.assertEqual(self.client("request", job), completed)
        self.assertEqual(len(self.human_events), 1, "the sole human send is the positive control")
        self.assertFalse(self.audit.exists(), "an external effect was attempted")
        self.assert_private_process()

    def assert_private_process(self):
        credential = self.credential.read_bytes().strip()
        db = sqlite3.connect(self.state / "private.sqlite3", isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")  # keep the shipped client alive while inspecting its argv
            with subprocess.Popen(self.cli("client", "--credential-file", self.credential, "events"), env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as client:
                try:
                    for pid in (self.service.pid, client.pid):
                        argv = subprocess.check_output(["ps", "-o", "command=", "-p", str(pid)])
                        self.assertTrue(argv.strip())
                        self.assertFalse(credential in argv or credential.split(b".")[1] in argv, "credential in argv")
                finally:
                    db.rollback()
                self.assertEqual(client.wait(timeout=5), 0)
        finally:
            db.close()
        self.assertEqual(stat.S_IMODE(self.endpoint.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.credential.stat().st_mode), 0o600)
        for path in (*self.root.rglob("*.log"), self.state / "private.sqlite3"):
            content = path.read_bytes()
            self.assertFalse(credential.split(b".")[1] in content, "raw credential on disk")
            if path.suffix == ".log":
                self.assertFalse(any(note.encode() in content for note in (TASK_TEXT, APPROVAL_TEXT, RESULT_TEXT)), "body in logs")

    def test_authentication_binding_and_revocation(self):
        credential = self.credential.read_text().strip()
        other = self.other.read_text().strip()
        valid = {"key": "only-status", "operation": "status"}
        unauthorized = (401, {"error": "unauthorized"})
        bad_request = (400, {"error": "invalid_request"})
        self.assertEqual(self.post("/v1/requests", valid), unauthorized)
        self.assertEqual(self.post("/v1/requests", valid, "reader." + credential.split(".")[1]), unauthorized)
        self.assertEqual(self.post("/v1/requests", {**valid, "profile": "reader"}, credential), bad_request)
        self.assertEqual(self.post("/v1/requests", {**valid, "destination": "human"}, credential), bad_request)
        self.assertEqual(self.post("/v1/requests", {**valid, "operation": "exec"}, credential), bad_request)
        self.assertEqual(self.post("/v1/requests", valid, credential, raw='{"key":"a","key":"b"}'), (400, {"error": "invalid_json"}))
        self.assertEqual(self.post("/v1/boss", {}, credential), (404, {"error": "not_found"}))
        self.assertEqual(self.post("/v1/requests", {**valid, "operation": "task", "text": TASK_TEXT}, other), (403, {"error": "operation_forbidden"}))

        task = self.client("task", "suspend-on-revoke", note=TASK_TEXT)
        status = self.client("status", "key")
        self.assertNotEqual(self.client("status", "key", credential=self.other)["request_id"], status["request_id"])
        self.run_cli("revoke", "xurupito")
        self.client("request", status["request_id"], error="unauthorized")
        self.client("events", error="unauthorized")
        self.assertEqual(self.run_cli("boss", "inbox"), {"requests": []})
        self.run_cli("boss", "accept", task["request_id"], error="operation_forbidden")
        replacement = self.root / "replacement.cred"
        self.run_cli("grant", "xurupito", "--credential-file", replacement)
        self.client("events", error="unauthorized")
        self.assertEqual(self.client("request", status["request_id"], credential=replacement), status)

        with closing(sqlite3.connect(self.state / "private.sqlite3")) as db, db:
            db.execute("UPDATE credentials SET expires_at = 0 WHERE profile = 'reader'")
        self.client("events", credential=self.other, error="unauthorized")
        self.assertFalse(self.audit.exists())

    def test_expired_cursor_can_advance_without_reopening_request(self):
        old = self.client("task", "expired-key", note=TASK_TEXT)
        self.run_cli("boss", "accept", old["request_id"])
        cursor = self.client("events")["next_cursor"]
        with closing(sqlite3.connect(self.state / "private.sqlite3")) as db, db:
            db.execute("UPDATE requests SET expires_at = 0 WHERE id = ?", (old["request_id"],))
        self.stop_service()
        self.start_service()
        self.client("request", old["request_id"], error="request_expired")
        self.client("task", "expired-key", note=TASK_TEXT, error="request_expired")
        self.client("approval", old["request_id"], "approve", error="request_expired")
        self.run_cli("boss", "request-approval", old["request_id"], note=APPROVAL_TEXT, error="request_expired")
        self.assertEqual(self.client("events")["events"], [])
        new = self.client("status", "fresh-key")
        self.assertEqual([e["request_id"] for e in self.client("events", "--cursor", cursor)["events"]], [new["request_id"]])
        with closing(sqlite3.connect(self.state / "private.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT fingerprint, payload FROM requests WHERE id = ?", (old["request_id"],)).fetchone(), ("", None))
            self.assertEqual(db.execute("SELECT data FROM events WHERE request_id = ?", (old["request_id"],)).fetchone(), (None,))

    def test_cursor_pages_preserve_profile_order(self):
        credential = self.credential.read_text().strip()
        expected = []
        for key in range(51):
            code, receipt = self.post("/v1/requests", {"key": str(key), "operation": "status"}, credential)
            self.assertEqual(code, 201)
            expected.append(receipt["request_id"])
            if key == 24:
                self.client("status", "other-profile", credential=self.other)
        first = self.client("events")
        self.assertTrue(first["has_more"])
        last = self.client("events", "--cursor", first["next_cursor"])
        self.assertFalse(last["has_more"])
        self.assertEqual([e["request_id"] for e in first["events"] + last["events"]], expected)
