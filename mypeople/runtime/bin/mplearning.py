"""Bounded notification evidence and versioned lessons. Contains no delivery code."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import uuid4

ACTIONS = {"silence", "notify_owner", "route_worker", "abstain"}
EVENTS = {"periodic", "merge", "ci_failure", "review", "direct_request"}
STATES = {"confirmed", "unknown", "duplicate", "closed", "active"}
OUTCOMES = {"confirmed", "rejected", "unknown", "not_attempted"}
ID = re.compile(r"[0-9a-f]{32}")
REPO = re.compile(r"[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}")
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
HEX = re.compile(r"[0-9a-f]{64}")
CAPABILITIES = {
    "owner": {"record", "propose", "transition", "lookup", "erase"},
    "provider": {"record"},
    "worker": {"record", "propose", "lookup"},
    "evaluator": {"transition", "lookup"},
}
CURRENT = """(SELECT state FROM transitions t WHERE (t.repo,t.role,t.lesson,t.version)
              =(l.repo,l.role,l.lesson,l.version) ORDER BY sequence DESC LIMIT 1)"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    incident TEXT NOT NULL, repo TEXT NOT NULL, role TEXT NOT NULL, origin TEXT NOT NULL,
    recorded INTEGER NOT NULL, expires INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(repo,role,incident)
);
CREATE TABLE IF NOT EXISTS lineages (
    lesson TEXT NOT NULL, repo TEXT NOT NULL, role TEXT NOT NULL, event TEXT NOT NULL,
    latest INTEGER NOT NULL, PRIMARY KEY(repo,role,lesson)
);
CREATE TABLE IF NOT EXISTS lessons (
    lesson TEXT NOT NULL, version INTEGER NOT NULL, incident TEXT NOT NULL,
    repo TEXT NOT NULL, role TEXT NOT NULL, event TEXT NOT NULL,
    origin TEXT NOT NULL, valid_from INTEGER NOT NULL, expires INTEGER NOT NULL,
    digest TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(repo,role,lesson,version),
    FOREIGN KEY(repo,role,incident) REFERENCES episodes(repo,role,incident) ON DELETE CASCADE,
    FOREIGN KEY(repo,role,lesson) REFERENCES lineages(repo,role,lesson)
);
CREATE TABLE IF NOT EXISTS transitions (
    sequence INTEGER PRIMARY KEY, repo TEXT NOT NULL, role TEXT NOT NULL,
    lesson TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, origin TEXT NOT NULL,
    recorded INTEGER NOT NULL,
    FOREIGN KEY(repo,role,lesson,version) REFERENCES lessons(repo,role,lesson,version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS lesson_scope ON lessons(repo,role,event,expires);
CREATE INDEX IF NOT EXISTS latest_transition ON transitions(repo,role,lesson,version,sequence DESC);
CREATE TRIGGER IF NOT EXISTS drop_empty_lineage AFTER DELETE ON lessons BEGIN
    DELETE FROM lineages WHERE (repo,role,lesson)=(OLD.repo,OLD.role,OLD.lesson)
      AND NOT EXISTS (SELECT 1 FROM lessons WHERE (repo,role,lesson)=(OLD.repo,OLD.role,OLD.lesson));
END;
"""


class Denied(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class Grant:
    origin: str
    scopes: frozenset

    def allows(self, op, repo, role):
        return op in CAPABILITIES[self.origin] and (repo, role) in self.scopes


def _matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _choice(target, choices):
    if not isinstance(target, str) or target not in choices:
        raise ValueError("invalid choice")
    return target


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer out of range")
    return value


def _shape(obj, fields):
    if not isinstance(obj, dict) or obj.keys() != set(fields):
        raise ValueError("unexpected fields")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def authenticate(path, token):
    """Read a private operator-configured grant file. Token values never enter the ledger."""
    if not _matches(HEX, token):
        raise Denied()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as grants:
            info = os.fstat(grants.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65_536:
                raise Denied()
            data = json.load(grants)
        digest = hashlib.sha256(token.encode()).hexdigest()
        rows = [row for key, row in data.items() if _matches(HEX, key) and hmac.compare_digest(key, digest)]
        if len(rows) != 1:
            raise Denied()
        row = rows[0]
        origin = _choice(row["origin"], CAPABILITIES)
        scopes = frozenset(tuple(scope) for scope in row["scopes"])
        if not all(len(scope) == 2 and _matches(REPO, scope[0]) and _matches(ROLE, scope[1]) for scope in scopes):
            raise Denied()
        return Grant(origin, scopes)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise Denied() from None


class LearningStore:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd) as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise Denied()
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.executescript(SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def execute(self, grant, request):
        _shape(request, ("op", "repo", "role", "data"))
        op, repo, role, data = (request[key] for key in ("op", "repo", "role", "data"))
        if not (_matches(REPO, repo) and _matches(ROLE, role) and isinstance(op, str) and grant.allows(op, repo, role)):
            raise Denied()
        at = int(self.clock())
        with self._connect() as conn:
            conn.execute("DELETE FROM episodes WHERE repo=? AND role=? AND expires<=?", (repo, role, at))
            conn.execute("DELETE FROM lessons WHERE repo=? AND role=? AND expires<=?", (repo, role, at))
            return getattr(self, "_" + op)(conn, grant.origin, repo, role, data, at)

    def _record(self, conn, origin, repo, role, data, at):
        _shape(data, ("incident", "event", "observed", "decision", "outcome", "evidence"))
        incident, evidence = data["incident"], data["evidence"]
        if not (_matches(ID, incident) and _matches(ID, evidence)):
            raise ValueError("opaque ID required")
        _choice(data["event"], EVENTS)
        _choice(data["observed"], STATES)
        _choice(data["decision"], ACTIONS)
        _choice(data["outcome"], OUTCOMES)
        body = _json(data)
        prev = conn.execute(
            "SELECT origin,body FROM episodes WHERE repo=? AND role=? AND incident=?", (repo, role, incident)
        ).fetchone()
        if prev:
            if tuple(prev) != (origin, body):
                raise Conflict()
            return {"incident": incident, "duplicate": True}
        lifetime = {"worker": 7, "provider": 30, "owner": 90}[origin] * 86_400
        conn.execute(
            "INSERT INTO episodes VALUES (?,?,?,?,?,?,?)", (incident, repo, role, origin, at, at + lifetime, body)
        )
        return {"incident": incident, "duplicate": False}

    def _propose(self, conn, origin, repo, role, data, at):
        _shape(data, ("lesson", "incident", "event", "condition", "action", "exceptions", "valid_from", "expires"))
        lesson, incident = data["lesson"], data["incident"]
        if not (_matches(ID, lesson) and _matches(ID, incident)):
            raise ValueError("opaque ID required")
        event = _choice(data["event"], EVENTS)
        _choice(data["condition"], STATES)
        _choice(data["action"], ACTIONS)
        exceptions = data["exceptions"]
        if not isinstance(exceptions, list) or len(exceptions) > len(STATES):
            raise ValueError("invalid exceptions")
        clean = {**data, "exceptions": sorted({_choice(state, STATES) for state in exceptions})}
        starts = _integer(data["valid_from"], 0, at + 30 * 86_400)
        expires = _integer(data["expires"], max(starts, at) + 1, at + 90 * 86_400)
        episode = conn.execute(
            "SELECT * FROM episodes WHERE repo=? AND role=? AND incident=?", (repo, role, incident)
        ).fetchone()
        if episode is None or json.loads(episode["body"])["event"] != event:
            raise Denied()
        last = conn.execute(
            "SELECT * FROM lineages WHERE repo=? AND role=? AND lesson=?", (repo, role, lesson)
        ).fetchone()
        if last and last["event"] != event:
            raise Conflict()
        version = last["latest"] + 1 if last else 1
        conn.execute(
            """INSERT INTO lineages VALUES (?,?,?,?,?) ON CONFLICT(repo,role,lesson) DO UPDATE SET latest=excluded.latest""",
            (lesson, repo, role, event, version),
        )
        digest = _digest(
            {"repo": repo, "role": role, "origin": origin, "version": version, "nonce": uuid4().hex, **clean}
        )
        conn.execute(
            "INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lesson, version, incident, repo, role, event, origin, starts, expires, digest, _json(clean)),
        )
        conn.execute(
            "INSERT INTO transitions VALUES (NULL,?,?,?,?,?,?,?)", (repo, role, lesson, version, "draft", origin, at)
        )
        return {"lesson": lesson, "version": version, "digest": digest}

    def _transition(self, conn, origin, repo, role, data, at):
        _shape(data, ("lesson", "version", "digest", "state"))
        lesson, version, state = data["lesson"], _integer(data["version"], 1, 2**31 - 1), data["state"]
        if not (_matches(ID, lesson) and _matches(HEX, data["digest"])):
            raise ValueError("version identity required")
        _choice(state, {"evaluated", "shadow", "active", "revoked", "rejected"})
        row = conn.execute(
            f"""SELECT l.*, {CURRENT} AS state FROM lessons l
                WHERE repo=? AND role=? AND lesson=? AND version=? AND digest=?""",
            (repo, role, lesson, version, data["digest"]),
        ).fetchone()
        if row is None:
            raise Denied()
        permitted = {
            ("draft", "evaluated"): "evaluator",
            ("draft", "rejected"): "evaluator",
            ("evaluated", "shadow"): "owner",
            ("shadow", "active"): "owner",
        }
        if state == "revoked" and row["state"] not in ("revoked", "rejected"):
            allowed = origin == "owner"
        else:
            allowed = permitted.get((row["state"], state)) == origin
        if not allowed:
            raise Denied()
        if state == "active":
            if not row["valid_from"] <= at < row["expires"]:
                raise Conflict()
            latest = conn.execute(
                "SELECT latest FROM lineages WHERE repo=? AND role=? AND lesson=?", (repo, role, lesson)
            ).fetchone()[0]
            if latest != version:
                raise Conflict()
            conn.execute(
                f"""INSERT INTO transitions(repo,role,lesson,version,state,origin,recorded)
                    SELECT l.repo,l.role,l.lesson,l.version,'revoked',?,? FROM lessons l
                    WHERE l.repo=? AND l.role=? AND l.lesson=? AND l.version<? AND {CURRENT}='active'""",
                (origin, at, repo, role, lesson, version),
            )
        conn.execute(
            "INSERT INTO transitions VALUES (NULL,?,?,?,?,?,?,?)", (repo, role, lesson, version, state, origin, at)
        )
        return {"lesson": lesson, "version": version, "state": state}

    def _lookup(self, conn, origin, repo, role, data, at):
        _shape(data, ("event", "observed", "exceptions"))
        event, observed = _choice(data["event"], EVENTS), _choice(data["observed"], STATES)
        if not isinstance(data["exceptions"], list) or len(data["exceptions"]) > len(STATES):
            raise ValueError("invalid exceptions")
        exceptions = {_choice(s, STATES) for s in data["exceptions"]}
        found = []
        for row in conn.execute(
            f"""SELECT l.*, e.origin AS label_origin FROM lessons l JOIN episodes e USING(repo,role,incident)
               WHERE l.repo=? AND l.role=? AND l.event=? AND l.valid_from<=? AND l.expires>?
                 AND {CURRENT}='active' ORDER BY l.lesson,l.version""",
            (repo, role, event, at, at),
        ):
            body = json.loads(row["body"])
            if body["condition"] == observed and not exceptions.intersection(body["exceptions"]):
                found.append(
                    {
                        "lesson": row["lesson"],
                        "version": row["version"],
                        "digest": row["digest"],
                        "action": body["action"],
                        "explicit": row["label_origin"] == "owner" and row["origin"] == "owner",
                    }
                )
        eligible = [row for row in found if row["explicit"]] or found
        actions = {row["action"] for row in eligible}
        return {
            "action": next(iter(actions)) if len(actions) == 1 else None,
            "reason": "match" if len(actions) == 1 else ("conflict" if actions else "no_match"),
            "consulted": [{k: row[k] for k in ("lesson", "version", "digest")} for row in eligible],
        }

    def _erase(self, conn, origin, repo, role, data, at):
        _shape(data, ("incident",))
        if not _matches(ID, data["incident"]):
            raise ValueError("opaque ID required")
        count = conn.execute(
            "DELETE FROM episodes WHERE repo=? AND role=? AND incident=?", (repo, role, data["incident"])
        ).rowcount
        return {"erased": bool(count)}
