#!/usr/bin/env python3
"""Opt-in local HTTP/JSON broker for the private Boss lane."""

import argparse
import fcntl
import http.client
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import stat
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

from store import ID, KEY, PROFILE, TOKEN, WORKER_TRANSITIONS, Rejected, Store, encode


BODY_LIMIT = 16 * 1024
NOTE_LIMIT = 4096
TIMEOUT = 3


def check_private(info, kind, mode):
    if not kind(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise Rejected(403, "unsafe_local_permissions")


def state_dir(path):
    if not path or not Path(path).is_absolute():
        raise Rejected(400, "absolute_state_dir_required")
    root = Path(path)
    root.mkdir(mode=0o700, exist_ok=True)
    check_private(root.lstat(), stat.S_ISDIR, 0o700)
    return root


def open_private(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        check_private(os.fstat(fd), stat.S_ISREG, 0o600)
    except Rejected:
        os.close(fd)
        raise
    return fd


def read_token(path):
    with os.fdopen(open_private(path, os.O_RDONLY), "rb") as source:
        credential = source.read(128).decode("ascii").strip()
    profile, _, token = credential.partition(".")
    if not PROFILE.fullmatch(profile) or not TOKEN.fullmatch(token):
        raise Rejected(401, "invalid_credential_file")
    return credential


def grant(store, args):
    token = secrets.token_urlsafe(32)
    fd = open_private(args.credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as target:
            target.write(f"{args.profile}.{token}\n")
            target.flush()
            os.fsync(target.fileno())
        store.grant(args.profile, token, args.ttl, args.allow_task)
    except (OSError, sqlite3.Error):
        args.credential_file.unlink()
        raise


def unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def checked_text(text):
    try:
        if isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= NOTE_LIMIT:
            return text
    except UnicodeError:
        pass
    raise Rejected(400, "invalid_text")


def stdin_text():
    try:
        return checked_text(sys.stdin.buffer.read(NOTE_LIMIT + 1).decode("utf-8"))
    except UnicodeError:
        raise Rejected(400, "invalid_text") from None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": "invalid_http_request"})

    def respond(self, code, obj):
        message = encode(obj).encode("ascii")
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(message)

    def read_body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
            raise Rejected(400, "invalid_content_length")
        size = int(lengths[0])
        if size > BODY_LIMIT:
            raise Rejected(413, "body_too_large")
        if self.headers.get_all("Content-Type") != ["application/json"]:
            raise Rejected(415, "expected_json")
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("truncated JSON")
            obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(obj, dict):
                raise ValueError("expected object")
            return obj
        except (ValueError, RecursionError):
            raise Rejected(400, "invalid_json") from None

    def handle_operation(self, store, db, identity, body, now):
        profile = identity["profile"]
        if self.path == "/v1/requests":
            key = body.get("key")
            operation = body.get("operation")
            if not matches(KEY, key):
                raise Rejected(400, "invalid_request")
            if operation == "status" and set(body) == {"key", "operation"}:
                return store.submit(db, identity, key, operation, {}, now)
            if operation == "task" and set(body) == {"key", "operation", "text"}:
                return store.submit(db, identity, key, operation, {"text": checked_text(body["text"])}, now)
            raise Rejected(400, "invalid_request")
        if self.path == "/v1/events":
            cursor = body.get("cursor")
            if set(body) != {"cursor"} or (cursor is not None and not matches(ID, cursor)):
                raise Rejected(400, "invalid_cursor")
            return 200, store.events(db, profile, cursor, now)
        if self.path == "/v1/request":
            request_id = body.get("request_id")
            if set(body) != {"request_id"} or not matches(ID, request_id):
                raise Rejected(400, "invalid_request")
            return 200, store.receipt(store.find_request(db, request_id, now, profile))
        if self.path == "/v1/approval":
            request_id, decision = body.get("request_id"), body.get("decision")
            if set(body) != {"request_id", "decision"} or not matches(ID, request_id) or decision not in ("approve", "decline"):
                raise Rejected(400, "invalid_approval")
            return 200, store.approve(db, identity, request_id, decision, now)
        raise Rejected(404, "not_found")

    def do_POST(self):
        try:
            headers = self.headers.get_all("Authorization", [])
            if len(headers) != 1:
                raise Rejected(401, "unauthorized")
            body = self.read_body()
            store = cast(Server, self.server).store
            with store.transaction() as db:
                now = int(time.time())
                identity = store.authenticate(db, headers[0], now)
                code, result = self.handle_operation(store, db, identity, body, now)
        except Rejected as error:
            code, result = error.code, {"error": error.reason}
        except sqlite3.Error:
            code, result = 503, {"error": "store_unavailable"}
        self.respond(code, result)


class Server(socketserver.UnixStreamServer):
    def __init__(self, endpoint, store):
        self.store = store
        self.next_expiry = time.monotonic()
        super().__init__(str(endpoint), Handler, bind_and_activate=False)
        try:
            mask = os.umask(0o177)  # Create the socket with mode 0600 even if killed during bind.
            try:
                self.server_bind()
            finally:
                os.umask(mask)
            self.server_activate()
        except OSError:
            self.server_close()
            raise

    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(TIMEOUT)
        return conn, address

    def handle_error(self, request, client_address):
        # In particular, never log raw HTTP parsing errors or exception values.
        pass

    def service_actions(self):
        now = time.monotonic()
        if now >= self.next_expiry:
            self.next_expiry = now + 60
            try:
                self.store.expire()
            except sqlite3.Error:
                print("private-route: expiry_unavailable", file=sys.stderr)


def serve(root, store):
    with os.fdopen(open_private(root / "serve.lock", os.O_RDWR | os.O_CREAT), "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected(409, "already_serving") from None
        endpoint = root / "route.sock"
        try:
            check_private(endpoint.lstat(), stat.S_ISSOCK, 0o600)
        except FileNotFoundError:
            pass
        else:
            endpoint.unlink()  # The lifetime lock excludes a cooperating live server.
        signal.signal(signal.SIGTERM, stop)
        try:
            with Server(endpoint, store) as server:
                server.serve_forever()
        finally:
            endpoint.unlink(missing_ok=True)


def stop(*args):
    raise SystemExit(0)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, endpoint):
        super().__init__("localhost", timeout=TIMEOUT)
        self.endpoint = endpoint

    def connect(self):
        check_private(self.endpoint.lstat(), stat.S_ISSOCK, 0o600)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT)
        self.sock.connect(str(self.endpoint))


def call(root, credential, path, body):
    conn = UnixConnection(root / "route.sock")
    try:
        conn.request("POST", path, body=encode(body), headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read(2 * 1024 * 1024))
        print(encode(result))
        return int(response.status >= 400)
    finally:
        conn.close()


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def positive(value):
    number = int(value)
    if not 0 < number <= 365 * 24 * 3600:
        raise argparse.ArgumentTypeError("expected a duration from 1 second to 365 days")
    return number


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=os.environ.get("MP_PRIVATE_ROUTE_STATE_DIR"))
    actions = parser.add_subparsers(dest="action", required=True)
    daemon = actions.add_parser("serve")
    daemon.add_argument("--request-ttl", type=positive, default=3600)
    allow = actions.add_parser("grant")
    allow.add_argument("profile")
    allow.add_argument("--credential-file", required=True, type=Path)
    allow.add_argument("--ttl", type=positive, default=3600)
    allow.add_argument("--allow-task", action="store_true")
    actions.add_parser("revoke").add_argument("profile")
    actions.add_parser("route").add_argument("state", choices=["on", "off"])
    client = actions.add_parser("client")
    client.add_argument("--credential-file", required=True, type=Path)
    commands = client.add_subparsers(dest="command", required=True)
    commands.add_parser("status").add_argument("key")
    commands.add_parser("task").add_argument("key", help="task text is read from stdin")
    commands.add_parser("events").add_argument("--cursor")
    commands.add_parser("request").add_argument("request_id")
    approval = commands.add_parser("approval")
    approval.add_argument("request_id")
    approval.add_argument("decision", choices=["approve", "decline"])
    boss = actions.add_parser("boss")
    boss_commands = boss.add_subparsers(dest="command", required=True)
    boss_commands.add_parser("inbox")
    for command in WORKER_TRANSITIONS:
        boss_commands.add_parser(command).add_argument("request_id")
    return parser.parse_args()


def run_client(root, args):
    credential = read_token(args.credential_file)
    if args.command == "status":
        return call(root, credential, "/v1/requests", {"operation": "status", "key": args.key})
    if args.command == "task":
        return call(root, credential, "/v1/requests", {"operation": "task", "key": args.key, "text": stdin_text()})
    if args.command == "events":
        return call(root, credential, "/v1/events", {"cursor": args.cursor})
    if args.command == "approval":
        return call(root, credential, "/v1/approval", {"request_id": args.request_id, "decision": args.decision})
    return call(root, credential, "/v1/request", {"request_id": args.request_id})


def run_boss(store, args):
    if args.command == "inbox":
        result = {"requests": store.boss_pending()}
    else:
        if not ID.fullmatch(args.request_id):
            raise Rejected(400, "invalid_request")
        data = {} if args.command == "accept" else {"text": stdin_text()}
        result = store.boss_update(args.command, args.request_id, data)
    print(encode(result))


def main():
    os.umask(0o077)
    args = arguments()
    root = state_dir(args.state_dir)
    if args.action == "client":
        return run_client(root, args)

    db_path = root / "private.sqlite3"
    with os.fdopen(open_private(db_path, os.O_CREAT | os.O_RDWR), "rb"):
        pass
    store = Store(db_path, getattr(args, "request_ttl", 3600))
    store.initialize()
    if args.action == "serve":
        serve(root, store)
    elif args.action == "route":
        store.set_route(args.state == "on")
    elif args.action == "boss":
        run_boss(store, args)
    else:
        if not PROFILE.fullmatch(args.profile):
            raise Rejected(400, "invalid_profile")
        if args.action == "grant":
            grant(store, args)
        else:
            store.revoke(args.profile)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Rejected as error:
        sys.exit(encode({"error": error.reason}))
    except (OSError, sqlite3.Error, http.client.HTTPException, ValueError):
        sys.exit('{"error":"local_operation_failed"}')
    except KeyboardInterrupt:
        sys.exit(0)
