"""Durable action queue shared by webhook, reconciliation and delivery workers."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhooks (id TEXT PRIMARY KEY, received REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prs (
    repo TEXT NOT NULL, number INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    dirty INTEGER NOT NULL DEFAULT 1, ready TEXT, proof TEXT, epoch INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0, terminal INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', token TEXT, deadline REAL, next REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, first_attempt REAL, reason TEXT, receipt TEXT
);
CREATE INDEX IF NOT EXISTS due_actions ON actions(status, next, created);
CREATE TABLE IF NOT EXISTS gaps (
    scope TEXT PRIMARY KEY, reason TEXT NOT NULL, since REAL NOT NULL, last REAL NOT NULL
);
"""


class Ledger:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.transaction() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _action(self, conn, key, kind, payload):
        conn.execute(
            "INSERT OR IGNORE INTO actions(id,kind,data,created) VALUES (?,?,?,?)",
            (key, kind, json.dumps(payload, sort_keys=True), self.clock()),
        )

    def ingest_board(self, events):
        accepted = []
        with self.transaction() as conn:
            for event in events:
                event_id, card, at = event["id"], event["card"], event["at"]
                if not re.fullmatch(r"[0-9a-f]{32}", event_id) or not re.fullmatch(r"[0-9a-f]{1,32}", card):
                    raise ValueError("invalid board event")
                payload = {"card": card, "at": float(at), "verified": event.get("verified") is True}
                self._action(conn, f"todo:{event_id}", "todo_done", payload)
                # The producer retains each event until this transaction is durable.
                # This cursor is diagnostic only: restores may replay older UUIDs.
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('journal_cursor',?)", (event_id,))
                accepted.append(event_id)
        return accepted

    def track(self, repo, number):
        return self.track_delivery(repo, {number}, None)

    def track_delivery(self, repo, numbers, delivery):
        with self.transaction() as conn:
            if (
                delivery
                and not conn.execute("INSERT OR IGNORE INTO webhooks VALUES (?,?)", (delivery, self.clock())).rowcount
            ):
                return False
            for number in numbers:
                conn.execute(
                    """INSERT INTO prs(repo,number) VALUES (?,?)
                       ON CONFLICT(repo,number) DO UPDATE SET revision=revision+1,dirty=1,retry_at=0,terminal=0""",
                    (repo, number),
                )
        return True

    def revision(self, repo, number):
        with self.transaction() as conn:
            return conn.execute("SELECT revision FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()[0]

    def remember(self, repo, number):
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO prs(repo,number) VALUES (?,?)", (repo, number))

    def tracked(self, *, dirty=False):
        with self.transaction() as conn:
            if dirty:
                rows = conn.execute(
                    "SELECT repo,number,revision FROM prs WHERE dirty=1 AND retry_at<=? ORDER BY repo,number",
                    (self.clock(),),
                )
            else:
                rows = conn.execute("SELECT repo,number,revision FROM prs WHERE terminal=0 ORDER BY repo,number")
            return [tuple(row) for row in rows]

    def gap(self, scope, reason, retry_delay=60):
        with self.transaction() as conn:
            at = self.clock()
            conn.execute(
                """INSERT INTO gaps VALUES (?,?,?,?) ON CONFLICT(scope)
                   DO UPDATE SET reason=excluded.reason,last=excluded.last""",
                (scope, reason, at, at),
            )
            if "#" in scope:
                repo, number = scope.split("#", 1)
                conn.execute(
                    "UPDATE prs SET dirty=1,retry_at=? WHERE repo=? AND number=?", (at + retry_delay, repo, int(number))
                )

    def clear_gap(self, scope):
        with self.transaction() as conn:
            conn.execute("DELETE FROM gaps WHERE scope=?", (scope,))

    def discovery_cutoff(self, repo):
        with self.transaction() as conn:
            key = f"discovery:{repo}"
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                at = self.clock()
                conn.execute("INSERT INTO meta VALUES (?,?)", (key, str(at)))
                conn.execute(
                    "INSERT OR IGNORE INTO gaps VALUES (?,?,?,?)",
                    (f"history:{repo}", "history_before_enrollment_uncovered", at, at),
                )
                return at
            return float(row[0])

    def discovery_completed(self, repo, at):
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"discovery:{repo}", str(at)))

    def sweep_completed(self):
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_sweep',?)", (str(self.clock()),))

    def _correction(self, conn, key, data):
        self._action(conn, f"correction:{key}", "correction", {**data, "superseded_at": self.clock()})

    def apply(self, repo, number, revision, snapshot, *, request_copilot=False):
        scope = f"{repo}#{number}"
        payload = {"repo": repo, "number": number, "head": snapshot["head"]}
        proof = ready_proof(snapshot)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
            if row is None or row["revision"] != revision:
                return False
            previous, epoch = row["ready"], row["epoch"]
            serialized = json.dumps(proof, sort_keys=True) if proof else None
            ready = previous
            if serialized != row["proof"]:
                if previous:
                    action = conn.execute("SELECT * FROM actions WHERE id=?", (previous,)).fetchone()
                    if action["status"] == "sent":
                        self._correction(conn, previous, json.loads(action["data"]))
                    conn.execute(
                        "UPDATE actions SET status='cancelled',reason='superseded' "
                        "WHERE id=? AND status IN ('pending','leased')",
                        (previous,),
                    )
                ready = None
                if proof:
                    epoch += 1
                    ready = f"ready:{scope}:{epoch}"
                    self._action(conn, ready, "ready", {**payload, "proof": proof})
            if snapshot["merged"]:
                merge = snapshot["merged"]
                self._action(conn, f"merge:{scope}:{merge}", "pr_merged", {**payload, "merge": merge})
                self._action(conn, f"merge-owner:{scope}:{merge}", "pr_merged_owner", {**payload, "merge": merge})
            if snapshot["open"]:
                for thread in snapshot["threads"]:
                    self._action(
                        conn, f"review:{scope}:{snapshot['head']}:{thread}", "review", {**payload, "thread": thread}
                    )
                copilot = snapshot["copilot"]
                request_key = f"copilot:{scope}:{snapshot['head']}"
                for reviewed_head, (submitted, review_id) in snapshot.get("copilot_history", {}).items():
                    conn.execute(
                        """UPDATE actions SET status='sent',reason=NULL,receipt=?
                           WHERE id=? AND status='unknown' AND first_attempt<=?""",
                        (f"exact_review_observed:{review_id}", f"copilot:{scope}:{reviewed_head}", submitted),
                    )
                outstanding = conn.execute(
                    "SELECT 1 FROM actions WHERE kind='copilot_request' AND status IN ('unknown','dispatching') "
                    "AND id GLOB ? LIMIT 1",
                    (f"copilot:{scope}:*",),
                ).fetchone()
                if request_copilot and not snapshot["draft"] and copilot == "none" and not outstanding:
                    self._action(conn, request_key, "copilot_request", payload)
                if copilot == "blocked":
                    self._action(
                        conn,
                        f"blocked:{scope}:{snapshot['head']}",
                        "blocked_notice",
                        {**payload, "reason": "copilot_provider_blocked"},
                    )
            conn.execute(
                "UPDATE prs SET revision=revision+1,dirty=0,retry_at=0,ready=?,proof=?,epoch=?,terminal=? WHERE repo=? AND number=?",
                (ready, serialized, epoch, bool(snapshot["merged"]), repo, number),
            )
            conn.execute("DELETE FROM gaps WHERE scope=?", (scope,))
        return True

    def claim(self, lane=None):
        at = self.clock()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE actions SET status='unknown',reason='dispatch_unconfirmed' "
                "WHERE status='dispatching' AND deadline<=?",
                (at,),
            )
            row = conn.execute(
                """SELECT * FROM actions
                   WHERE ((status='pending' AND next<=?) OR (status='leased' AND deadline<=?))
                     AND (? IS NULL OR (kind='todo_done') = ?)
                   ORDER BY created,rowid LIMIT 1""",
                (at, at, lane, lane == "todo"),
            ).fetchone()
            if row is None:
                return None
            action = {**dict(row), "data": json.loads(row["data"]), "token": uuid4().hex}
            conn.execute(
                "UPDATE actions SET status='leased',token=?,deadline=? WHERE id=?",
                (action["token"], at + 60, action["id"]),
            )
            return action

    def begin(self, action, *, revision=None):
        at = self.clock()
        with self.transaction() as conn:
            if revision is not None:
                data = action["data"]
                row = conn.execute(
                    "SELECT revision FROM prs WHERE repo=? AND number=?", (data["repo"], data["number"])
                ).fetchone()
                if row is None or row[0] != revision:
                    return False
            return bool(
                conn.execute(
                    """UPDATE actions SET status='dispatching',deadline=?,attempts=attempts+1,
                   first_attempt=COALESCE(first_attempt,?)
                   WHERE id=? AND token=? AND status='leased' AND deadline>?""",
                    (at + 120, at, action["id"], action["token"], at),
                ).rowcount
            )

    def finish(self, action, status, *, reason=None, receipt=None, delay=0):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id=? AND token=?", (action["id"], action["token"])
            ).fetchone()
            if row is None or row["status"] not in ("leased", "dispatching", "unknown"):
                return False
            if row["status"] == "leased" and row["deadline"] <= self.clock():
                return False
            if status == "sent" and row["status"] == "leased":
                # Only a fenced dispatch may record a successful side effect.
                return False
            if row["status"] == "unknown" and status != "sent":
                return False
            conn.execute(
                "UPDATE actions SET status=?,reason=?,receipt=?,next=? WHERE id=?",
                (status, reason, receipt, self.clock() + delay, action["id"]),
            )
            data = action["data"]
            if action["kind"] == "ready" and status == "sent":
                current = conn.execute(
                    "SELECT ready FROM prs WHERE repo=? AND number=?", (data["repo"], data["number"])
                ).fetchone()
                if current is None or current[0] != action["id"]:
                    self._correction(conn, action["id"], data)
            if status == "blocked" and action["kind"] == "copilot_request":
                key = f"blocked:{data['repo']}#{data['number']}:{data['head']}"
                self._action(conn, key, "blocked_notice", {**data, "reason": reason})
            return True

    def status(self):
        # Summaries deliberately exclude action payloads and provider response text.
        with self.transaction() as conn:
            counts = dict(conn.execute("SELECT status,COUNT(*) FROM actions GROUP BY status"))
            cursor = conn.execute("SELECT value FROM meta WHERE key='journal_cursor'").fetchone()
            sweep = conn.execute("SELECT value FROM meta WHERE key='last_sweep'").fetchone()
            return {
                "actions": counts,
                "last_sweep": float(sweep[0]) if sweep else None,
                "journal_cursor": cursor[0] if cursor else None,
                "webhooks": conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0],
                "gaps": [dict(r) for r in conn.execute("SELECT * FROM gaps ORDER BY scope")],
                "attention": [
                    dict(r)
                    for r in conn.execute(
                        """SELECT id,status,attempts,reason,created FROM actions
                       WHERE status IN ('blocked','unknown') ORDER BY created LIMIT 100"""
                    )
                ],
                "max_first_attempt_seconds": conn.execute(
                    "SELECT MAX(first_attempt-created) FROM actions WHERE first_attempt IS NOT NULL"
                ).fetchone()[0],
            }


def ready_proof(snapshot):
    if not (snapshot["open"] and not snapshot["draft"] and snapshot["approval"] and snapshot["checks"]):
        return None
    return {
        "head": snapshot["head"],
        "base": snapshot["base"],
        "approval": snapshot["approval"],
        "checks": snapshot["checks"],
    }
