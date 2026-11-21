"""Board-server reliability and performance pins.

These cover the failures measured against todo-server at 551a713: Nagle's algorithm
stalling keep-alive responses, idle connections pinning threads with no cap, request
bodies with no size limit, session cookies that never expire, writes that rehash the
whole board, and board reads holding the global lock while the socket writes.
"""

import json
import os
import socket
import threading
import time
import http.client
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sandbox


ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "mypeople" / "runtime" / "bin"
SECRET = "secret"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerMixin:
    """One in-process todo-server on a random port, sqlite board, machine secret."""

    env = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mod = sandbox.load(cls.tmp.name, "todo-server.py", MYPEOPLE_BOARD_BACKEND="sqlite", **cls.env)
        server_cls = getattr(cls.mod.C, "BoundedThreadingHTTPServer", None)
        if server_cls is None:
            from http.server import ThreadingHTTPServer as server_cls
        cls.httpd = server_cls(("127.0.0.1", 0), cls.mod.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, secret=SECRET, cookie=None, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Queue-Secret"] = secret
        if cookie:
            headers["Cookie"] = cookie
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        cookie_out = resp.getheader("Set-Cookie")
        conn.close()
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = None
        return resp.status, parsed, cookie_out


class NagleAndTimeoutTests(ServerMixin, unittest.TestCase):
    """Keep-alive responses must not wait on delayed ACK, and an idle socket must die."""

    env = {"MP_HANDLER_TIMEOUT": "0.4", "MP_MAX_CONNECTIONS": "128"}

    def test_accepted_socket_disables_nagle_and_has_a_timeout(self):
        seen = {}
        orig = self.mod.Handler.setup

        def setup(handler):
            orig(handler)
            seen["nodelay"] = handler.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            seen["timeout"] = handler.connection.gettimeout()

        self.mod.Handler.setup = setup
        try:
            status, body, _ = self.request("GET", "/health")
        finally:
            self.mod.Handler.setup = orig
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}["status"], body["status"])
        # Linux reports 1, macOS reports the flag bit (4); any non-zero value means on.
        self.assertTrue(seen["nodelay"], "TCP_NODELAY must be set on the accepted socket")
        self.assertIsNotNone(seen["timeout"])
        self.assertLessEqual(seen["timeout"], 0.4)

    def test_idle_connection_is_closed_by_the_handler_timeout(self):
        socks = []
        for _ in range(6):
            so = socket.create_connection(("127.0.0.1", self.port))
            so.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
            socks.append(so)
        time.sleep(1.0)
        alive = 0
        for so in socks:
            so.settimeout(0.05)
            try:
                data = so.recv(1)
                if data:
                    alive += 1
            except socket.timeout:
                alive += 1
            finally:
                so.close()
        self.assertEqual(0, alive, "partial requests must be reaped by the handler timeout")


class ConnectionCapTests(ServerMixin, unittest.TestCase):
    env = {"MP_HANDLER_TIMEOUT": "5", "MP_MAX_CONNECTIONS": "2"}

    def test_connections_past_the_cap_are_rejected_without_a_handler_thread(self):
        held = []
        for _ in range(2):
            so = socket.create_connection(("127.0.0.1", self.port))
            so.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
            held.append(so)
        time.sleep(0.2)
        before = threading.active_count()
        status, body, _ = self.request("GET", "/health", timeout=2)
        after = threading.active_count()
        for so in held:
            so.close()
        self.assertEqual(503, status)
        self.assertEqual("over_cap", (body or {}).get("error"))
        self.assertLess(after, before + 2, "the rejected connection must not keep a thread")


class BodyLimitTests(ServerMixin, unittest.TestCase):
    env = {"MP_MAX_JSON_BYTES": "128", "MP_MAX_UPLOAD_BYTES": "256", "MP_HANDLER_TIMEOUT": "5"}

    def _raw_post(self, payload, secret=SECRET, content_type="application/json"):
        so = socket.create_connection(("127.0.0.1", self.port))
        so.settimeout(3)
        headers = [
            "POST /todo/update HTTP/1.1",
            "Host: x",
            "Content-Type: %s" % content_type,
            "Content-Length: %d" % len(payload),
            "Connection: close",
        ]
        if secret:
            headers.append("X-Queue-Secret: %s" % secret)
        so.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
        data = b""
        while True:
            try:
                chunk = so.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        so.close()
        return data.split(b"\r\n", 1)[0].decode()

    def test_small_add_still_works(self):
        status, body, _ = self.request("POST", "/todo/update", {"op": "add", "text": "ok", "test": True})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])

    def test_json_over_the_configured_cap_is_413_and_is_not_parsed(self):
        line = self._raw_post(b'{"op":"add","text":"' + b"x" * 200 + b'","test":true}')
        self.assertIn("413", line)
        status, board, _ = self.request("GET", "/todo/board")
        self.assertEqual(200, status)
        texts = [t.get("text") for t in board["tasks"].values()]
        self.assertNotIn("x" * 200, "".join(texts), "an oversized add must not land")

    def test_unauthenticated_oversized_post_is_401_without_reading_the_body(self):
        so = socket.create_connection(("127.0.0.1", self.port))
        so.settimeout(1.5)
        so.sendall(b"POST /todo/update HTTP/1.1\r\nHost: x\r\nContent-Length: 8000000\r\n\r\n")
        line = so.recv(200).split(b"\r\n", 1)[0].decode()
        so.close()
        self.assertIn("401", line)

    def test_proof_upload_over_its_own_cap_is_413(self):
        payload = b"x" * 300
        so = socket.create_connection(("127.0.0.1", self.port))
        so.settimeout(3)
        so.sendall(
            b"POST /todo/proof HTTP/1.1\r\nHost: x\r\nX-Queue-Secret: secret\r\n"
            b"Content-Type: multipart/form-data; boundary=b\r\n"
            b"Content-Length: %d\r\n\r\n" % len(payload) + payload
        )
        line = so.recv(300).split(b"\r\n", 1)[0].decode()
        so.close()
        self.assertIn("413", line)


class SessionCookieTests(unittest.TestCase):
    """A page-load session is how the owner UI logs in, and it must not live forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = sandbox.load(self.tmp.name, "todo-server.py", MYPEOPLE_BOARD_BACKEND="sqlite", MP_SESSION_TTL="120")
        server_cls = getattr(self.mod.C, "BoundedThreadingHTTPServer", None)
        if server_cls is None:
            from http.server import ThreadingHTTPServer as server_cls
        self.httpd = server_cls(("127.0.0.1", 0), self.mod.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _req(self, method, path, body=None, cookie=None, secret=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if secret:
            headers["X-Queue-Secret"] = secret
        if cookie:
            headers["Cookie"] = cookie
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = None
        return resp.status, parsed, set_cookie

    def test_loopback_page_sets_an_expiring_cookie_that_authorizes_a_write(self):
        status, _, set_cookie = self._req("GET", "/")
        self.assertEqual(200, status)
        self.assertTrue(set_cookie.startswith("mp_session="), set_cookie)
        self.assertIn("Max-Age=120", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        token = set_cookie.split(";", 1)[0]
        self.assertTrue(self.mod.C.valid_session(token.split("=", 1)[1]))
        status, body, _ = self._req(
            "POST", "/todo/update", {"op": "add", "text": "from the page", "test": True}, cookie=token
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"], "the owner UI still writes with the page session")

    def test_expired_or_tampered_session_is_rejected(self):
        stale = self.mod.C.mint_session(ttl=30, now=time.time() - 120)
        self.assertFalse(self.mod.C.valid_session(stale))
        status, body, _ = self._req(
            "POST", "/todo/update", {"op": "add", "text": "stale", "test": True}, cookie="mp_session=" + stale
        )
        self.assertEqual(401, status)
        nonce, exp, sig = stale.split(".")
        flipped = "%s.%d.%s" % (nonce, int(exp) + 99999, sig)
        self.assertFalse(self.mod.C.valid_session(flipped))
        self.assertFalse(self.mod.C.valid_session("legacy.signature-without-expiry"))

    def test_no_cookie_and_no_secret_is_still_401(self):
        status, _, _ = self._req("GET", "/todo/board")
        self.assertEqual(401, status)

    def test_default_bind_is_loopback_unless_configured(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ.pop("BIND_ADDR", None)
            mod = sandbox.load(td, "mpcommon.py")
            self.assertEqual("127.0.0.1", mod.CFG["BIND_ADDR"])


class WriteScalingTests(ServerMixin, unittest.TestCase):
    """A write persists the cards it touched and must not rehash the rest of the board."""

    env = {"MP_HANDLER_TIMEOUT": "5"}

    def _seed(self, n):
        board = {"version": 2, "order": [], "pinSeq": 0, "tasks": {}}
        now = time.time()
        for i in range(n):
            tid = "c%03d" % i
            board["tasks"][tid] = {
                "id": tid,
                "text": "seed %d" % i,
                "state": "working",
                "assignee": "",
                "comments": [{"id": "k%d" % i, "by": "CEO", "body": "c" * 80, "ts": now}],
                "proofs": [],
                "test": True,
                "created": now,
                "updated": now,
            }
            board["order"].append(tid)
        self.mod.save_board(board)
        # Drop the in-process snapshot so the request reloads from disk, as a restart would.
        if hasattr(self.mod, "_SNAP"):
            self.mod._SNAP = None
            self.mod._SNAP_JSON = None

    def test_add_hashes_only_the_new_card_and_survives_a_fresh_connection(self):
        self._seed(40)
        hashed = []
        orig = self.mod.BS.task_hash

        def counting(task):
            hashed.append(task.get("id"))
            return orig(task)

        self.mod.BS.task_hash = counting
        try:
            status, body, _ = self.request("POST", "/todo/update", {"op": "add", "text": "just one", "test": True})
        finally:
            self.mod.BS.task_hash = orig
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual([body["id"]], hashed, "save must hash the new card, not all %d" % (40 + len(hashed)))
        fresh = self.mod.BS.load_board(self.mod._sqlite_path())
        self.assertEqual("just one", fresh["tasks"][body["id"]]["text"])
        self.assertEqual(41, len(fresh["tasks"]))
        self.assertEqual(
            "ok", self.mod.BS.connect(self.mod._sqlite_path()).execute("PRAGMA integrity_check").fetchone()[0]
        )
        con = self.mod.BS.connect(self.mod._sqlite_path())
        try:
            mode, sync = (
                con.execute("PRAGMA journal_mode").fetchone()[0],
                con.execute("PRAGMA synchronous").fetchone()[0],
            )
        finally:
            con.close()
        self.assertEqual("wal", mode)
        self.assertEqual(2, sync)  # FULL

    def test_unsaved_mutation_does_not_leak_into_the_next_load(self):
        board = {
            "version": 2,
            "order": ["a1"],
            "pinSeq": 0,
            "tasks": {"a1": {"id": "a1", "text": "original", "state": "working"}},
        }
        # Replace whatever an earlier test stored. The shrink guard would refuse this
        # write on a larger board, which is a different contract.
        self.assertTrue(self.mod.BS.save_board(self.mod._sqlite_path(), board, shrink_guard=False))
        if hasattr(self.mod, "_SNAP"):
            self.mod._SNAP = None
            self.mod._SNAP_JSON = None
        loaded = self.mod.load_board()
        loaded["tasks"]["a1"]["text"] = "dirty"
        self.assertEqual("original", self.mod.load_board()["tasks"]["a1"]["text"])

    def test_a_refused_shrink_leaves_the_stored_board_and_the_snapshot(self):
        self._seed(8)
        board = self.mod.load_board()
        board["tasks"] = dict(list(board["tasks"].items())[:2])
        board["order"] = list(board["tasks"])
        self.assertFalse(self.mod.save_board(board))
        self.assertEqual(8, len(self.mod.BS.load_board(self.mod._sqlite_path())["tasks"]))
        self.assertEqual(8, len(self.mod.load_board()["tasks"]))


class ReadDoesNotBlockWriteTests(ServerMixin, unittest.TestCase):
    env = {"MP_HANDLER_TIMEOUT": "5"}

    def test_a_slow_board_read_does_not_hold_the_writer_lock(self):
        self.mod.save_board(
            {
                "version": 2,
                "order": ["a1"],
                "pinSeq": 0,
                "tasks": {
                    "a1": {"id": "a1", "text": "seed", "state": "working", "comments": [], "proofs": [], "test": True}
                },
            }
        )
        if hasattr(self.mod, "_SNAP"):
            self.mod._SNAP = None
            self.mod._SNAP_JSON = None
        started = threading.Event()
        release = threading.Event()
        orig = self.mod.Handler._send

        def slow(handler, code, obj=None, raw=None, **kw):
            blob = raw.encode("utf-8") if isinstance(raw, str) else raw
            boardish = (isinstance(obj, dict) and "tasks" in obj) or (
                isinstance(blob, (bytes, bytearray)) and b'"tasks"' in blob
            )
            if boardish and code == 200:
                started.set()
                release.wait(2)
            return orig(handler, code, obj, raw=raw, **kw)

        self.mod.Handler._send = slow
        try:
            reader = threading.Thread(target=lambda: self.request("GET", "/todo/board", timeout=4), daemon=True)
            reader.start()
            self.assertTrue(started.wait(2), "the board read did not reach the socket write")
            t0 = time.perf_counter()
            try:
                status, body, _ = self.request(
                    "POST", "/todo/update", {"op": "add", "text": "while reading", "test": True}, timeout=0.8
                )
            except (socket.timeout, TimeoutError, OSError):
                status, body = None, None
            elapsed = time.perf_counter() - t0
        finally:
            release.set()
            self.mod.Handler._send = orig
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertLess(elapsed, 0.8, "the writer waited on the in-flight read")


class ExternalRestoreTests(ServerMixin, unittest.TestCase):
    """board-restore writes the database from another process. The live snapshot must notice."""

    env = {"MP_HANDLER_TIMEOUT": "5"}

    def test_a_restore_replaces_the_cached_board_and_later_edits_keep_it(self):
        status, body, _ = self.request("POST", "/todo/update", {"op": "add", "text": "live", "test": True})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        restored = {
            "version": 2,
            "order": ["r1"],
            "pinSeq": 0,
            "tasks": {
                "r1": {
                    "id": "r1",
                    "text": "restored-only",
                    "state": "working",
                    "comments": [],
                    "proofs": [],
                    "test": True,
                }
            },
        }
        db = self.mod._sqlite_path()
        self.assertTrue(self.mod.BS.save_board(db, restored, shrink_guard=False))
        self.mod.BS.note_external_write(db)
        status, board, _ = self.request("GET", "/todo/board")
        self.assertEqual(200, status)
        self.assertEqual(["restored-only"], [t.get("text") for t in board["tasks"].values()])
        status, added, _ = self.request("POST", "/todo/update", {"op": "add", "text": "after", "test": True})
        self.assertEqual(200, status)
        fresh = self.mod.BS.load_board(db)
        self.assertEqual("restored-only", fresh["tasks"]["r1"]["text"])
        self.assertIn(added["id"], fresh["tasks"])

    def test_board_restore_marks_both_backends(self):
        text = (BIN / "board-restore").read_text(encoding="utf-8")
        self.assertEqual(2, text.count("BS.note_external_write("))


class QueueBodyLimitTests(unittest.TestCase):
    """The queue uses the same handler. An oversized body must be 413, not an empty success."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = sandbox.load(self.tmp.name, "queue-server.py", MP_MAX_JSON_BYTES="64", MP_HANDLER_TIMEOUT="5")
        server_cls = getattr(self.mod.C, "BoundedThreadingHTTPServer", None)
        if server_cls is None:
            from http.server import ThreadingHTTPServer as server_cls
        self.httpd = server_cls(("127.0.0.1", 0), self.mod.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _post(self, path, payload, secret=SECRET):
        so = socket.create_connection(("127.0.0.1", self.port))
        so.settimeout(3)
        headers = [
            "POST %s HTTP/1.1" % path,
            "Host: x",
            "Content-Type: application/json",
            "Content-Length: %d" % len(payload),
            "Connection: close",
        ]
        if secret:
            headers.append("X-Queue-Secret: %s" % secret)
        so.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
        data = b""
        while True:
            chunk = so.recv(4096)
            if not chunk:
                break
            data += chunk
        so.close()
        return data.split(b"\r\n", 1)[0].decode()

    def test_a_small_heartbeat_is_accepted(self):
        line = self._post("/heartbeat", b'{"hostname":"h1"}')
        self.assertIn("200", line)
        self.assertIn("h1", self.mod.CLIENTS)

    def test_an_oversized_heartbeat_is_413_and_stores_nothing(self):
        line = self._post("/heartbeat", b'{"hostname":"huge","pad":"' + b"x" * 80 + b'"}')
        self.assertIn("413", line)
        self.assertNotIn("huge", self.mod.CLIENTS)

    def test_an_oversized_task_result_does_not_complete_the_task(self):
        self.mod.TASKS["t1"] = {"task_id": "t1", "type": "x", "ok": None, "result": None}
        line = self._post("/task/result", b'{"task_id":"t1","ok":true,"result":"' + b"y" * 80 + b'"}')
        self.assertIn("413", line)
        self.assertIsNone(self.mod.TASKS["t1"]["ok"])


class BindDefaultTests(unittest.TestCase):
    def test_a_fresh_queue_env_binds_loopback(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from mypeople import firstrun

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "queue.env"
            with (
                mock.patch.object(firstrun, "CONFIG_PATH", str(path)),
                mock.patch.object(firstrun, "CONFIG_DIR", td),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("BIND_ADDR", None)
                firstrun.write_queue_env(str(Path(td) / "state"), "claude")
            text = path.read_text()
        self.assertIn('BIND_ADDR="127.0.0.1"', text)


if __name__ == "__main__":
    unittest.main()
