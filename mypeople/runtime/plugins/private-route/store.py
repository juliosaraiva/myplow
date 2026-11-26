"""Durable requests and their private return stream."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager


PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
ID = re.compile(r"[0-9a-f]{32}")
PAGE_SIZE = 50
WORKER_TRANSITIONS = {
    "accept": ("queued", "accepted", "task"),
    "request-approval": ("accepted", "awaiting_approval", "approval_required"),
    "result": ("approved", "complete", "result"),
}


class Rejected(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class Store:
    def __init__(self, path, request_ttl):
        self.path = path
        self.request_ttl = request_ttl

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        finally:
            db.close()  # Rolls back any uncommitted transaction on failure.

    def initialize(self):
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY, enabled INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS credentials (
                profile TEXT PRIMARY KEY, digest TEXT NOT NULL, expires_at INTEGER NOT NULL,
                allow_task INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, profile TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, operation TEXT NOT NULL, expires_at INTEGER NOT NULL,
                phase TEXT NOT NULL, payload TEXT, UNIQUE(profile, request_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL REFERENCES requests(id), kind TEXT NOT NULL,
                data TEXT, UNIQUE(request_id, kind))""")
            db.execute("CREATE INDEX IF NOT EXISTS requests_expiry ON requests(expires_at)")

    def grant(self, profile, token, ttl, allow_task):
        with self.transaction() as db:
            db.execute(
                """INSERT INTO credentials VALUES (?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET digest=excluded.digest,
                expires_at=excluded.expires_at, allow_task=excluded.allow_task""",
                (profile, digest(token), int(time.time()) + ttl, allow_task),
            )

    def revoke(self, profile):
        with self.transaction() as db:
            db.execute("DELETE FROM credentials WHERE profile = ?", (profile,))

    def set_route(self, enabled):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('boss', ?)", (enabled,))

    @staticmethod
    def authenticate(db, authorization, now):
        scheme, _, credential = authorization.partition(" ")
        profile, separator, token = credential.partition(".")
        if scheme != "Bearer" or not separator or not PROFILE.fullmatch(profile) or not TOKEN.fullmatch(token):
            raise Rejected(401, "unauthorized")
        record = db.execute("SELECT * FROM credentials WHERE profile = ?", (profile,)).fetchone()
        expected = record["digest"] if record else "0" * 64
        if not hmac.compare_digest(digest(token), expected) or record is None or record["expires_at"] <= now:
            raise Rejected(401, "unauthorized")
        return record

    @staticmethod
    def require_route(db):
        route = db.execute("SELECT enabled FROM settings WHERE name = 'boss'").fetchone()
        if route is None or not route["enabled"]:
            raise Rejected(503, "route_unavailable")

    @staticmethod
    def add_event(db, request_id, kind, data):
        db.execute(
            "INSERT INTO events (id, request_id, kind, data) VALUES (?, ?, ?, ?)",
            (secrets.token_hex(16), request_id, kind, encode(data)),
        )

    def submit(self, db, identity, key, operation, payload, now):
        if operation == "task" and not identity["allow_task"]:
            raise Rejected(403, "operation_forbidden")
        self.require_route(db)
        profile = identity["profile"]
        fingerprint = digest(encode([operation, payload]))
        previous = db.execute(
            "SELECT * FROM requests WHERE profile = ? AND request_key = ?", (profile, key)
        ).fetchone()
        if previous:
            if previous["expires_at"] <= now or not previous["fingerprint"]:
                raise Rejected(410, "request_expired")
            if previous["fingerprint"] != fingerprint:
                raise Rejected(409, "idempotency_conflict")
            return 200, self.receipt(previous)

        request_id = secrets.token_hex(16)
        expires_at = now + self.request_ttl
        phase = "complete" if operation == "status" else "queued"
        db.execute(
            "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, profile, key, fingerprint, operation, expires_at, phase, encode(payload) if payload else None),
        )
        if operation == "status":
            self.add_event(db, request_id, "status", {"route": "available"})
        return 201, {"request_id": request_id, "expires_at": expires_at, "phase": phase}

    @staticmethod
    def receipt(row):
        return {"request_id": row["id"], "expires_at": row["expires_at"], "phase": row["phase"]}

    @staticmethod
    def find_request(db, request_id, now, profile=None):
        if profile is None:
            row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM requests WHERE profile = ? AND id = ?", (profile, request_id)).fetchone()
        if not row:
            raise Rejected(404, "not_found")
        if row["expires_at"] <= now or not row["fingerprint"]:
            raise Rejected(410, "request_expired")
        return row

    def approve(self, db, identity, request_id, decision, now):
        row = self.find_request(db, request_id, now, identity["profile"])
        if not identity["allow_task"] or row["operation"] != "task":
            raise Rejected(403, "operation_forbidden")
        next_phase = "approved" if decision == "approve" else "declined"
        return self.transition(db, row, "awaiting_approval", next_phase, "approval", {"decision": decision})

    def boss_pending(self):
        with self.transaction() as db:
            self.require_route(db)
            rows = db.execute(
                """SELECT requests.* FROM requests JOIN credentials USING (profile)
                WHERE operation = 'task' AND requests.expires_at > :now
                AND credentials.expires_at > :now AND credentials.allow_task = 1
                AND payload IS NOT NULL
                AND phase IN ('queued', 'accepted', 'awaiting_approval', 'approved')
                ORDER BY requests.rowid LIMIT :limit""",
                {"now": int(time.time()), "limit": PAGE_SIZE},
            ).fetchall()
            return [dict(self.receipt(row), profile=row["profile"], task=json.loads(row["payload"])) for row in rows]

    def boss_update(self, action, request_id, data):
        with self.transaction() as db:
            now = int(time.time())
            row = self.find_request(db, request_id, now)
            if row["operation"] != "task":
                raise Rejected(403, "operation_forbidden")
            if db.execute(
                "SELECT 1 FROM credentials WHERE profile = ? AND allow_task = 1 AND expires_at > ?",
                (row["profile"], now),
            ).fetchone() is None:
                raise Rejected(403, "operation_forbidden")
            return self.transition(db, row, *WORKER_TRANSITIONS[action], data)

    def transition(self, db, row, expected_phase, next_phase, kind, data):
        self.require_route(db)
        previous = db.execute("SELECT data FROM events WHERE request_id = ? AND kind = ?", (row["id"], kind)).fetchone()
        if previous:
            if previous["data"] != encode(data):
                raise Rejected(409, "idempotency_conflict")
            return self.receipt(row)
        if row["phase"] != expected_phase:
            raise Rejected(409, "invalid_phase")
        db.execute("UPDATE requests SET phase = ? WHERE id = ?", (next_phase, row["id"]))
        self.add_event(db, row["id"], kind, data)
        return dict(self.receipt(row), phase=next_phase)

    @staticmethod
    def events(db, profile, cursor, now):
        seq = 0
        if cursor:
            row = db.execute(
                "SELECT seq FROM events JOIN requests ON requests.id = request_id WHERE events.id = ? AND profile = ?",
                (cursor, profile),
            ).fetchone()
            if row is None:
                raise Rejected(404, "not_found")
            seq = row["seq"]
        rows = db.execute(
            """SELECT events.id, request_id, kind, data FROM events JOIN requests ON requests.id = request_id
            WHERE profile = ? AND seq > ? AND expires_at > ? AND data IS NOT NULL ORDER BY seq LIMIT ?""",
            (profile, seq, now, PAGE_SIZE + 1),
        ).fetchall()
        events = [
            {"id": row["id"], "request_id": row["request_id"], "type": row["kind"], "data": json.loads(row["data"])}
            for row in rows[:PAGE_SIZE]
        ]
        return {"events": events, "next_cursor": events[-1]["id"] if events else cursor, "has_more": len(rows) > PAGE_SIZE}

    def expire(self):
        now = int(time.time())
        with self.transaction() as db:
            db.execute(
                "UPDATE requests SET payload = NULL, fingerprint = '' WHERE fingerprint != '' AND expires_at <= ?", (now,)
            )
            db.execute(
                """UPDATE events SET data = NULL WHERE data IS NOT NULL
                AND request_id IN (SELECT id FROM requests WHERE expires_at <= ?)""",
                (now,),
            )
