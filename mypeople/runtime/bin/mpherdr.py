"""Private local transport for the opt-in Herdr pilot."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
from uuid import uuid4


class TransportError(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
    label TEXT NOT NULL, binding TEXT
);
CREATE TABLE IF NOT EXISTS sends (
    agent TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY(agent,id)
);
"""


class Pilot:
    def __init__(self, endpoint, state):
        self.path = Path(endpoint)
        self.state = Path(state)
        if not self.path.is_absolute() or self.path.name != "herdr.sock":
            raise TransportError("absolute Herdr socket required")
        if self.path.parent.name in ("main", "mc-main", "default") or self.path.parent.parent.name != "sessions":
            raise TransportError("a separate named Herdr session is required")
        self.identity = self._socket_identity()

    def _socket_identity(self):
        try:
            info = self.path.lstat()
        except OSError:
            raise TransportError("Herdr endpoint missing") from None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TransportError("Herdr endpoint must be a private local socket")
        return (info.st_dev, info.st_ino)

    def rpc(self, method, **params):
        if self._socket_identity() != self.identity:
            raise TransportError("Herdr endpoint changed")
        ident = uuid4().hex
        request = {"id": ident, "method": method, "params": params}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(4)
                peer.connect(str(self.path))
                peer.sendall(json.dumps(request).encode() + b"\n")
                with peer.makefile("rb") as stream:
                    line = stream.readline(1_048_577)
                if len(line) > 1_048_576 or not line.endswith(b"\n"):
                    raise ValueError
                response = json.loads(line)
        except (OSError, ValueError):
            raise TransportError("Herdr acknowledgement unavailable") from None
        if (
            not isinstance(response, dict)
            or response.get("id") != ident
            or not isinstance(response.get("result"), dict)
        ):
            raise TransportError("Herdr refused or returned an invalid acknowledgement")
        return response["result"]

    @contextmanager
    def db(self):
        self.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.state, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd) as entry:
            info = os.fstat(entry.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise TransportError("Herdr state must be private")
        conn = sqlite3.connect(self.state, timeout=5)
        try:
            with conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def spawn(self, agent, cwd, launch):
        label = "mp-" + uuid4().hex
        with self.db() as conn:
            if conn.execute("SELECT 1 FROM agents WHERE id=?", (agent,)).fetchone():
                raise TransportError("agent ID already reserved; reconnect instead")
            conn.execute("INSERT INTO agents VALUES (?,?,?,?,?,NULL)", (agent, str(self.path), *self.identity, label))
        result = self.rpc("workspace.create", cwd=cwd, label=label)
        pane = result["root_pane"]
        binding = {key: pane[key] for key in ("pane_id", "workspace_id", "tab_id", "terminal_id")}
        # Bind the execution to the workspace returned by this creation, never to focus.
        if result["workspace"]["workspace_id"] != binding["workspace_id"]:
            raise TransportError("workspace and pane identity disagree")
        renamed = self.rpc("pane.rename", pane_id=binding["pane_id"], label=label).get("pane", {})
        if renamed.get("label") != label or any(renamed.get(key) != value for key, value in binding.items()):
            raise TransportError("Herdr launch identity changed")
        if (
            self.rpc("pane.send_input", pane_id=binding["pane_id"], text="exec " + launch, keys=["Enter"]).get("type")
            != "ok"
        ):
            raise TransportError("unconfirmed Herdr launch")
        with self.db() as conn:
            conn.execute("UPDATE agents SET binding=? WHERE id=?", (json.dumps(binding), agent))
        return self.lookup(agent)

    def lookup(self, agent):
        with self.db() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
        if row is None or not row["binding"]:
            raise TransportError("agent has no confirmed Herdr binding")
        if (row["endpoint"], row["device"], row["inode"]) != (str(self.path), *self.identity):
            raise TransportError("agent belongs to a different Herdr endpoint")
        binding = json.loads(row["binding"])
        pane = self.rpc("pane.get", pane_id=binding["pane_id"]).get("pane", {})
        if any(pane.get(key) != value for key, value in binding.items()) or pane.get("label") != row["label"]:
            raise TransportError("Herdr pane identity changed")
        return pane

    def attach(self, agent, executable):
        if not os.path.isabs(executable) or not os.access(executable, os.X_OK):
            raise TransportError("set MYPEOPLE_HERDR_BIN to the pinned absolute executable")
        pane = self.lookup(agent)
        env = {**os.environ, "HERDR_SOCKET_PATH": str(self.path)}
        env.pop("HERDR_SESSION", None)
        return subprocess.run([executable, "terminal", "attach", pane["terminal_id"]], env=env).returncode

    def read(self, agent):
        pane = self.lookup(agent)
        view = self.rpc("pane.read", pane_id=pane["pane_id"], source="recent", lines=80)
        if view.get("type") != "pane_read" or not isinstance(view.get("read"), dict):
            raise TransportError("Herdr read response unavailable")
        view = view["read"]
        if not isinstance(view.get("text"), str):
            raise TransportError("Herdr read response unavailable")
        if any(view.get(key) != pane[key] for key in ("pane_id", "workspace_id", "tab_id")):
            raise TransportError("Herdr read identity changed")
        return view["text"]

    def send(self, agent, delivery, message):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", delivery or ""):
            raise TransportError("Herdr send requires --delivery-id")
        if not message or len(message.encode()) > 32_768 or any(ord(c) < 32 and c != "\n" for c in message):
            raise TransportError("Herdr message contains unsupported input")
        pane = self.lookup(agent)
        digest = hashlib.sha256(message.encode()).hexdigest()
        with self.db() as conn:
            prior = conn.execute("SELECT digest,status FROM sends WHERE agent=? AND id=?", (agent, delivery)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise TransportError("delivery ID already used for different text")
                if prior["status"] == "sent":
                    return "already_sent"
                raise TransportError("delivery outcome unknown; reconcile before any new send")
            if pane.get("agent_status") != "idle":
                raise TransportError("Herdr agent is not idle")
            conn.execute("INSERT INTO sends VALUES (?,?,?,'unknown')", (agent, delivery, digest))
        try:
            acknowledgement = self.rpc("pane.send_input", pane_id=pane["pane_id"], text=message, keys=["Enter"])
            if acknowledgement.get("type") != "ok":
                raise TransportError("unexpected input acknowledgement")
        except TransportError:
            raise TransportError("delivery outcome unknown; no automatic resend") from None
        with self.db() as conn:
            conn.execute("UPDATE sends SET status='sent' WHERE agent=? AND id=?", (agent, delivery))
        return "sent"

    def close(self, agent):
        pane = self.lookup(agent)
        self.rpc("pane.close", pane_id=pane["pane_id"])
        # Keep the reserved identity and delivery tombstones for the life of the disposable install.
        with self.db() as conn:
            conn.execute("UPDATE agents SET binding=NULL WHERE id=?", (agent,))
