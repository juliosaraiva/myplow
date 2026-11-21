#!/usr/bin/env python3
"""Plow Chat plugin: the owner texts the Boss over iMessage/SMS through Plow.

Turn it on with PLOW_CHAT=1 in queue.env; `mypeople up` then keeps it running.
First run has no credentials, so it mints a code: run `plow-chat.py status`
and text "Plow Activate: <code>" to the number it prints, once.

Usage:
  plow-chat.py serve                  run the bridge (supervise.sh entry point)
  plow-chat.py reply "text"           reply in the owner's own chat
  plow-chat.py reply cht_x "text"     reply in that chat (groups)
  plow-chat.py reply cht_x --file a.jpg "text"   send photos/files too (up to 4)
  plow-chat.py react cht_x msg_y like tapback a message (love, laugh, an emoji, remove...)
  plow-chat.py api POST /v1/chats '{...}'  any Plow API call with this line's login
                                     (docs: https://api.plow.co/openapi.json)
  plow-chat.py status                 print activation/bridge state

The Boss is told what plain text loses: the message a text replies to, who it
mentions, and tapbacks people left. A tapback never wakes the Boss on its own;
it rides along with the next text.

One account covers every chat: the owner can add the Plow line to group
threads, and each group is its own chat that reaches the Boss and is answered
in the thread that asked.

In a Plow cloud agent (PLOW_API_BASE set, no saved login) there is nothing to
activate: the VM's own line is the chat, the token comes from the environment
(or the "proxied" placeholder the exe.dev proxy swaps for the real one), and
the chats come from GET /v1/agents/me.

The bridge holds Plow's account websocket (POST /v1/ws/ticket, then /v1/ws): a frame
says which chat changed and that chat is read at once. Every (re)connect, and every
minute, a pass over all chats catches whatever the socket missed; while the socket
is down the bridge polls every PLOW_CHAT_POLL_SECONDS.
"""

import http.client
import fcntl
import json
import hashlib
import mimetypes
import os
import re
import shlex
import sqlite3
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from mpcommon import SEND_TIMEOUT, WebSocket, plugin_text  # noqa: E402  (the one discord-chat holds Discord's gateway with)

BASE = (os.environ.get("PLOW_CHAT_BASE_URL") or os.environ.get("PLOW_API_BASE") or "https://api.plow.co").rstrip("/")
INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME") or Path.home() / "mypeople")
STATE_DIR = Path(os.environ.get("PLOW_CHAT_STATE_DIR") or INSTALL / "state" / "plow-chat")
CREDS = STATE_DIR / "creds.json"  # {"token": ..., "chat_uid": ..., "base_url"?: ...}
# A saved login may name its own server: the second bridge supervise.sh runs for a WhatsApp lab
# (whatsapp-linq-api) learns where the lab is from this one file, so a cloud VM needs nothing else.
try:
    BASE = (json.loads(CREDS.read_text()).get("base_url") or BASE).rstrip("/")
except (OSError, ValueError, AttributeError):
    pass
ACTIVATION = STATE_DIR / "activation.json"
ATTACHMENTS = STATE_DIR / "attachments"  # inbound photos/files, for the Boss to open
STATE = STATE_DIR / "state.json"  # {"marks": {chat: [created_at, uid]}, "seeded_chats": [...]}
INBOX_DB = STATE_DIR / "inbox.sqlite3"  # durable inbound dispatch records; marks only pane-accepted handoffs
INBOX_LOCK_DIR = STATE_DIR / "inbox-locks"
# How every reply looks, in one file the owner or the Boss edits (Daniel 10-18: "the style of how to reply should be
# part of the message package"). It rides in every message package; "{name}" becomes the agent that gets the text.
STYLE = STATE_DIR / "style.md"
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
# PLOW_CHAT_ROUTER=jev: one JEV decision picks which live agent (mp status) gets each text; the last agent and
# the quoted message are context only. Off, everything goes to the Boss. In an exehome VM, TYPESAFE_URL is the
# proxy integration that adds the key, so the key never enters the VM; on a Mac, TYPESAFE_API_KEY.
ROUTER = os.environ.get("PLOW_CHAT_ROUTER", "")
JEV_URL = os.environ.get("TYPESAFE_URL") or "https://api.typesafe.ai/v1/systemone"
# Tuned on 23 multi-turn conversations (74/74) and 12 hard cases x3 (36/36): follow-ups, topic switches, vague lines,
# agents named mid-chat or in a greeting, and requests that refer back to another agent's answer.
ROUTE_ASK = (
    "A person texts one WhatsApp chat answered by several AI agents, as in a group chat. Which agent should get "
    "this message? If it calls an agent by name, even in a greeting (hey coach, chef?, Mailer, ...), pick that "
    "agent. If it asks for one agent's kind of work (an email, a recipe, a workout), pick that agent, even "
    "mid-conversation and even when it refers back to what another agent just said. If it asks about this "
    "system, which agents exist, or making, changing or retiring an agent, pick Boss. Only if it has no topic of "
    "its own and names no agent (a greeting, ok, thanks, yes, and?, more, why?, what can you do?): pick the agent "
    "who answered in the last few minutes, or Boss when none did."
)
FOLLOW_MINUTES = float(
    os.environ.get("PLOW_CHAT_FOLLOW_MINUTES", "30")
)  # an older answer no longer counts as the conversation
# A voice note is heard before it is routed, so the router and the agent get words, not only a file (Daniel 10-20). The
# server at PLOW_CHAT_STT_URL takes the audio as multipart "audio" and answers {"text": ...} (the homelab's stt-api
# /transcribe/fast). Unset, down or slow: the file still arrives, with a note that the transcript is missing.
STT_URL = os.environ.get("PLOW_CHAT_STT_URL", "")
STT_SECONDS = 30
# Who wrote a reply: each agent's prompt signs its last bubble "~Name" on its own line (Daniel 10-18). Unsigned
# bubbles of the same reply name no one.
SPEAKER = re.compile(r"(?:\n|^)~([A-Z][A-Za-z0-9]{1,19})\s*$")


def signed_by(body: str) -> str:
    named = SPEAKER.search(body or "")
    return named.group(1) if named else ""


# A pass over every chat runs on each (re)connect, every SWEEP_SECONDS on a live socket (Plow's
# events are best-effort, so a frame can go missing), and every POLL_SECONDS while the socket is
# down or a text waits on the Boss.
POLL_SECONDS = float(os.environ.get("PLOW_CHAT_POLL_SECONDS", "5"))
SWEEP_SECONDS = 60
PING_SECONDS = 30  # a socket that answered nothing for two of these is dead, whatever TCP says
SELF = str(Path(__file__).resolve())
# How the Boss runs this plugin. A second bridge, on another Plow account or another Plow-shaped
# server (a WhatsApp number's whatsapp-linq-api), runs with its own state dir and base URL; the
# commands it hands the Boss carry both, or a reply goes out under the wrong login or to the wrong server.
RUN = (
    "".join(
        f"{k}={shlex.quote(os.environ[k])} " for k in ("PLOW_CHAT_STATE_DIR", "PLOW_CHAT_BASE_URL") if os.environ.get(k)
    )
    + f"python3 {SELF}"
)
STATE_LOCK = threading.Lock()
ROUTE_LOCKS_GUARD = threading.Lock()
ROUTE_LOCKS = {}
WELCOME = "MyPlow is up. Text me what you want done."


def log(msg: str):
    # Router data is private message content. Callers must emit metadata only; this
    # final guard also strips filesystem paths and bearer-like values from errors.
    msg = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", str(msg))
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] plow-chat: {msg}", flush=True)


READER = []  # this process's one kept-alive connection for reads


def api(method: str, path: str, body=None, token=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method == "GET":
        return read(path, headers, timeout)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def read(path: str, headers: dict, timeout: float):
    """GET over one kept-alive connection: a fresh TLS handshake per read was ~1s of every text's
    wait. Only reads reuse it, because only a read is safe to resend when the server closed the
    idle connection under it."""
    u = urllib.parse.urlsplit(BASE)
    for retry in (False, True):
        if not READER:
            conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            READER.append(conn(u.netloc, timeout=timeout))
        try:
            READER[0].request("GET", u.path + path, headers=headers)
            r = READER[0].getresponse()
            raw = r.read()
            break
        except (OSError, http.client.HTTPException) as e:
            READER.pop().close()
            if retry:
                raise OSError(f"GET {path}: {e!r}") from e
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        if r.status < 400:
            raise
        return r.status, {}


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def write_json(p: Path, obj):
    # Creds hold a user-wide bearer token: 600 from the first byte, never chmod after.
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def listing(data) -> list:
    return data.get("data") or data.get("messages") or data.get("chats") or [] if isinstance(data, dict) else data


# --- Boss ---

# "You hold" comes first, because an agent that only hears about reply/react told the owner "I can't create threads" (10-15): it holds
# this line's login, and with it the whole API.
STAMP = "[plowchat] from {sender} in {chat}:"
TEMPLATE = (
    "{seen_line}" + STAMP + " {their_message}{attachments}{context}\n"
    "{owner_line}"
    "(You hold this line's Plow login and with it the whole Plow API (docs: {api_docs}). "
    "Any call: {plow} api METHOD /v1/PATH ['JSON body']; e.g. {plow} api GET /v1/agents/me "
    '(this line and its chats), or {plow} api POST /v1/chats \'{"line_uid": "ln_...", '
    '"members": ["+1..."], "body": "first text", "idempotency_key": "any-unique-id"}\' '
    "to start a new thread or group. Do what the person asks with it; never answer that you "
    "can't without trying. This is the owner's Plow messages line. The owner texts here, and so "
    "does anyone they added to this thread. To answer, run: {reply_command} — it goes back to that "
    "same thread, which may be a group. Plain text only, no markdown. To send "
    "photos or files, add --file PATH before the text, up to 4.{tapback})"
    "{style}"
)


def envelope(
    who: str,
    text: str,
    chat_uid: str,
    files=(),
    context=(),
    mid: str = "",
    agent: str = "",
    owner: bool = False,
    delivery_id: str = "",
) -> str:
    # Plow seats whoever the carrier reports in the thread, so the Boss is told
    # who actually spoke and which thread — in a group these differ per message.
    sent = "".join(f"\n[attached, open it: {f}]" for f in files)
    said = "".join(f"\n({c})" for c in context)
    tap = (
        (
            f" To tapback this message, run: {RUN} react {chat_uid} {mid} like "
            f"(or love, laugh, emphasize, question, dislike, or one emoji)."
        )
        if mid
        else ""
    )
    # The owner's ask: feedback the moment an agent sees his text, not after the slow turn. It is
    # the first line, so it is the first command run, not one noticed mid-work (Boss, 10-05).
    seen = (
        (
            f"FIRST, before reading on: {RUN} react {chat_uid} {mid} 👀"
            f"  (marks this text seen for the sender; then read, work and reply)"
        )
        if mid
        else ""
    )
    if seen and delivery_id:
        seen += f" [stable delivery id: {delivery_id}; repeated ID means same inbound event]"
    if seen:
        seen += "\n"
    try:
        style = " ".join(STYLE.read_text().split()).replace("{name}", agent or BOSS_AGENT.rsplit(":", 1)[1])
    except OSError:
        style = ""
    return plugin_text(
        "plow-chat",
        TEMPLATE,
        {
            "seen_line": seen,
            "sender": who,
            "chat": chat_uid,
            "their_message": text,
            "attachments": sent,
            "context": said,
            "owner_line": f"(This is the owner. {OWNER_RULE})\n" if owner else "",
            "api_docs": f"{BASE}/openapi.json",
            "plow": RUN,
            "reply_command": f'{RUN} reply {chat_uid} "your reply"',
            "tapback": tap,
            "style": f"\n(Reply style: {style})" if style else "",
        },
    )


OWNER_RULE = (
    "When the owner addresses you, it is a task: carry it to the end unless they say otherwise. Never treat it as "
    "just conversation. Absolute rule."
)
TAPBACKS = {
    "love": "loved",
    "like": "liked",
    "dislike": "disliked",
    "laugh": "laughed at",
    "emphasize": "emphasized",
    "question": "questioned",
}


def speaker(sender: dict) -> str:
    if sender.get("type") == "agent" and sender.get("relationship") == "self":
        return "you"  # this line, i.e. the Boss
    return sender.get("display_name") or sender.get("provider_key") or "chat member"


def whose(m: dict) -> str:
    """`your message msg_x: "..."` or `Dan's message msg_x: "..."`."""
    who = speaker(m.get("sender") or {})
    text = " ".join((m.get("body") or "").split())
    text = (text[:500] + "…" if len(text) > 500 else text) or "(a photo or file)"
    owner = "your" if who == "you" else f"{who}'s"
    return f'{owner} message {m.get("uid")}: "{text}"'


def context(message: dict) -> list:
    """What plain text loses: the message this one answers, and who it tags. [] for neither."""
    lines = []
    if (message.get("reply_to") or {}).get("message"):
        lines.append(f"in reply to {whose(message['reply_to']['message'])}")
    tags = [("you" if t.get("is_me") else t.get("handle") or "someone") for t in message.get("mentions") or []]
    if tags:
        lines.append("mentions: " + ", ".join(dict.fromkeys(tags)))
    return lines


def note_tapbacks(msgs: list, chat_uid: str, seed: bool = True):
    """Hold new tapbacks from people for the Boss's next message; never wake it for one.

    A thumbs-up is not someone writing to the Boss: waking it would make every
    acknowledgement a turn. So it rides along with the next real text, tied to
    the message it landed on.
    ponytail: only the page each poll already fetches (the newest 20 messages)
    is read, so a tapback on an older message is never seen. Upgrade path: the
    reaction_added frames on the account websocket.
    """
    live = {}
    for m in msgs:
        for r in m.get("reactions") or []:
            actor = r.get("actor") or {}
            if actor.get("type") != "member":
                continue  # the Boss's own tapbacks come back on the page too
            verb = TAPBACKS.get(r.get("type")) or f"reacted {r.get('custom_emoji') or ''} to"
            # The type is in the key: turning a like into a heart may keep the reaction's uid.
            key = f"{r.get('uid')}:{r.get('type')}:{r.get('custom_emoji') or ''}"
            # It only ever rides along with a real text, so it must not read as a verdict on that text:
            # "not a new message" under Patrick's "8*5" made the Boss skip a real question (10-06).
            live[key] = (
                f"earlier tapback in {chat_uid}, separate from the new text above: {speaker(actor)} {verb} {whose(m)}"
            )
    with STATE_LOCK:
        st = read_json(STATE, {})
        # Per chat, exactly the tapbacks on its page now: one window shared by every chat let a
        # quiet chat's old tapbacks age out and come back as news (delattre1/mypeople#40).
        by_chat = st.setdefault("page_tapbacks", {})
        known = by_chat.get(chat_uid)
        new = [k for k in live if k not in (known or [])]
        if not new and known is not None and len(known) == len(live):
            return
        # First sight of a chat with seeding on: its tapbacks are history, not news. A chat seen
        # before this per-chat record existed is seeded too, once.
        first = known is None and (seed or chat_uid in st.get("tapback_chats", []))
        news = [] if first else [live[k] for k in new]
        st["pending_tapbacks"] = (st.get("pending_tapbacks", []) + news)[-20:]
        by_chat[chat_uid] = sorted(live)
        write_json(STATE, st)


def role_about(role: str) -> str:
    """What an agent does, from its role (name@version -> roles/profiles/<name>/<version>.json): the profile's
    one-line metadata.description, else its personality text."""
    try:
        key, version = role.split("@", 1)
        profile = json.loads((INSTALL / "roles" / "profiles" / key / f"{version}.json").read_text())
        if (profile.get("metadata") or {}).get("description"):
            return str(profile["metadata"]["description"])[:600]
        text = (INSTALL / "roles" / profile["spec"]["personalityRef"]).read_text()
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return " ".join(ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#"))[:600]


def live_agents() -> dict:
    """{name: (agent id, what it does)} for this host's live agents: the router's candidates. Created = in, retired = out."""
    out = subprocess.run([sys.executable, MP_BIN, "status"], capture_output=True, text=True, timeout=15).stdout
    return {
        aid.rsplit(":", 1)[1]: (aid, role_about(role))
        for aid, role in re.findall(r"^(\S+:\S+)\s+\[alive\s*\]\s+(\S+)", out, re.M)
        if aid.startswith(f"{HOST_ID}/")
    }


def pick_agent(text: str, message: dict, last: str = "", minutes=None, last_reply: str = "") -> str:
    """The one routing decision: which live agent gets this text. Who answered last (the name the chat's latest
    reply starts with), how many minutes ago and what they said are context only. Anything unsure or broken goes
    to the Boss."""
    if ROUTER != "jev":
        return BOSS_AGENT
    try:
        agents = live_agents()
        if (
            not text
        ):  # a photo, or a voice note nobody could hear, has nothing to classify: it stays with whoever had the chat
            return agents[last][0] if last in agents else BOSS_AGENT
        if len(agents) < 2:
            return BOSS_AGENT
        state = {"message": text[:2000]}
        if last in agents:
            state["last_answered_by"] = last
            if minutes is not None:
                state["minutes_since_that_answer"] = round(minutes, 1)
            if last_reply:
                state["that_answer"] = last_reply[:300]
        quoted = ((message.get("reply_to") or {}).get("message") or {}).get("body")
        if quoted:
            state["replying_to"] = quoted[:500]
        body = {
            "state": state,
            "model": "jev-latest",
            "questions": {
                "agent": {
                    "type": "choice",
                    "instructions": ROUTE_ASK,
                    "criteria": {n: about or n for n, (_, about) in agents.items()},
                }
            },
        }
        headers = {"Content-Type": "application/json"}
        if os.environ.get("TYPESAFE_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["TYPESAFE_API_KEY"]
        req = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            choice = json.load(r)["answers"]["agent"]["choice"]
    except Exception as e:  # noqa: BLE001 - a router that fails must never lose the text
        log(f"router failed ({type(e).__name__}); falling back to Boss")
        return BOSS_AGENT
    return agents[choice][0] if choice in agents else BOSS_AGENT


def send_to_agent(message: str, agent: str = BOSS_AGENT):
    """True: `mp send` reported pane handoff, not agent-read/completion. False: explicit refusal.
    None: unknown; it may have reached the pane, so recovery reuses its stable delivery ID."""
    try:
        r = subprocess.run(
            [sys.executable, MP_BIN, "send", agent, message], capture_output=True, text=True, timeout=SEND_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ERROR mp send ({type(e).__name__})")
        return None
    if r.returncode != 0:
        log(f"ERROR mp send rc={r.returncode}")
        return False
    return True


WARN_AFTER = 120  # seconds a text may wait for a Boss that is coming back before its sender is told
NOT_YET = {
    "en": "Your text reached me but I could not wake the team with it. Retrying; nothing is lost.",
    "pt": "Sua mensagem chegou aqui, mas ainda não consegui passá-la para a equipe. Vou tentar de novo; nada se perdeu.",
    "es": "Tu mensaje llegó, pero todavía no pude pasárselo al equipo. Lo intento de nuevo; no se perdió nada.",
}
# Words only one of these languages uses a lot. Short texts often carry none of them: then English.
WORDS = {
    "pt": set(
        "não nao você voce vc tá tô eu meu minha minhas meus cadê cade aqui isso esse essa tem obrigado "
        "obrigada oi olá também tambem consegue quero onde agora então entao pra pro né nós".split()
    ),
    "es": set(
        "qué cómo dónde usted tú está estoy gracias hola sí muy pero también puedes quiero aquí eso esto "
        "ahora entonces nosotros".split()
    ),
}


def language(text: str) -> str:
    """The language a sender wrote in, of the ones there is a reply for: pt, es, or en."""
    low = text.lower()
    words = re.findall(r"[^\W\d_]+", low)
    score = {
        lang: sum(w in ws for w in words) + 2 * bool(re.search(marks, low))
        for lang, ws, marks in (("pt", WORDS["pt"], "[ãõç]"), ("es", WORDS["es"], "[ñ¿¡]"))
    }
    best = max(score, key=score.get)
    return best if score[best] else "en"


STARTED = time.time()  # a bridge that just started (a reboot, an update) gives the Boss WARN_AFTER too


def message_age(message: dict) -> float:
    try:
        sent = datetime.fromisoformat((message.get("created_at") or "").replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - sent).total_seconds()
    except (ValueError, TypeError):
        return float("inf")  # no readable time: treat it as long overdue


FLOOR = ["", ""]  # a mark below every message: everything after it routes


def order(message: dict) -> list:
    """A message's place in its chat: [created_at as fixed-width UTC, uid]. Parsed, because servers
    differ (api.plow.co sends microseconds, whatsapp-linq-api milliseconds)."""
    ts = message.get("created_at") or ""
    try:
        ts = (
            datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        )
    except ValueError:
        pass
    return [ts, message.get("uid") or ""]


def route_lock(chat_uid: str):
    """Serialize route attempts for one chat across threads and bridge processes."""
    digest = hashlib.sha256(chat_uid.encode("utf-8")).hexdigest()
    with ROUTE_LOCKS_GUARD:
        lock = ROUTE_LOCKS.setdefault(chat_uid, threading.Lock())
    return _RouteLockContext(lock, INBOX_LOCK_DIR / (digest + ".lock"))


class _RouteLockContext:
    def __init__(self, local_lock, path):
        self.local_lock, self.path, self.fh = local_lock, path, None

    def __enter__(self):
        self.local_lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
            self.fh = os.fdopen(fd, "a+")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self.local_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fh:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
        finally:
            self.local_lock.release()


INBOX_SCHEMA_VERSION = 4
INBOX_OPEN_LOCK = threading.Lock()
STALE_ALERTED = set()


def _inbox_schema_sql(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='inbound_delivery'").fetchone()
    return _schema_normalize(row[0]) if row else ""


def _schema_normalize(sql):
    return re.sub(r"\s+", "", sql.lower())


def _inbox_ddl(payload_default=""):
    payload = f"message_json TEXT NOT NULL DEFAULT '{payload_default}'," if payload_default is not None else ""
    return f"""CREATE TABLE inbound_delivery (
        chat_uid TEXT NOT NULL,
        message_uid TEXT NOT NULL,
        {payload}
        order_ts TEXT NOT NULL,
        order_uid TEXT NOT NULL,
        delivery_id TEXT NOT NULL UNIQUE,
        attempt_id TEXT NOT NULL DEFAULT '',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL CHECK(state IN ('pending','dispatching','ambiguous','refused','handoff_accepted')),
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY(chat_uid, message_uid)
    )"""


def _create_inbox_table(db):
    db.execute(_inbox_ddl())


def _cleanup_ddl():
    return """CREATE TABLE attachment_cleanup (
        path TEXT PRIMARY KEY,
        created_at REAL NOT NULL
    )"""


def _create_cleanup_table(db):
    db.execute(_cleanup_ddl())


def _manifest_ddl():
    return """CREATE TABLE attachment_manifest (
        chat_uid TEXT NOT NULL,
        message_uid TEXT NOT NULL,
        attachment_uid TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,
        PRIMARY KEY(chat_uid,message_uid,attachment_uid)
    )"""


def _manifest_schema_sql(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='attachment_manifest'").fetchone()
    return _schema_normalize(row[0]) if row else ""


def _create_manifest_table(db):
    db.execute(_manifest_ddl())


def _cleanup_schema_sql(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='attachment_cleanup'").fetchone()
    return _schema_normalize(row[0]) if row else ""


def _migrate_accepted_cleanup(db):
    # Kept as a migration hook; exact ownership is resolved by v4 manifest scan.
    return


def _migrate_v3_attachments(db):
    # v3 cleanup jobs came from a prefix scan and may name another message's file.
    # Drop those untrusted jobs; preserve all files as safe orphans.
    db.execute("DELETE FROM attachment_cleanup")
    rows = db.execute(
        "SELECT chat_uid,message_uid,message_json FROM inbound_delivery WHERE message_json!=''"
    ).fetchall()
    refs = {}
    message_uids = {}
    for chat_uid, message_uid, payload in rows:
        try:
            items = json.loads(payload).get("attachments") or []
        except (TypeError, json.JSONDecodeError, AttributeError):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("uid"):
                legacy = _legacy_attachment_name(item)
                owner = (chat_uid, message_uid, str(item["uid"]))
                refs.setdefault(legacy, []).append(owner)
                message_uids.setdefault((chat_uid, message_uid), []).append(str(item["uid"]))
    duplicate_owners = {
        owner
        for owners in refs.values()
        for owner in owners
        if message_uids.get((owner[0], owner[1]), []).count(owner[2]) > 1
    }
    try:
        _preflight_attachment_dir(create=False)
    except FileNotFoundError:
        refs = {}
    for path, owners in refs.items():
        if len(owners) != 1 or owners[0] in duplicate_owners:
            log("ALERT: ambiguous legacy attachment ownership; preserving orphan")
            continue
        try:
            st = (ATTACHMENTS / path).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1:
            db.execute(
                "INSERT INTO attachment_manifest(chat_uid,message_uid,attachment_uid,path) VALUES(?,?,?,?)",
                (*owners[0], path),
            )
    accepted = db.execute("SELECT chat_uid,message_uid FROM inbound_delivery WHERE state='handoff_accepted'").fetchall()
    for chat_uid, message_uid in accepted:
        _queue_attachment_cleanup(db, chat_uid, message_uid)
    db.execute("UPDATE inbound_delivery SET message_json='' WHERE state='handoff_accepted'")


def _secure_inbox_files():
    for path in (INBOX_DB, Path(str(INBOX_DB) + "-wal"), Path(str(INBOX_DB) + "-shm")):
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        # SQLite can unlink a sidecar while another process is doing this
        # lstat. Tolerate a zero-link inode only for WAL/SHM, retaining strict
        # type, owner, and permission checks and rejecting multi-link files.
        is_sidecar = path.name.endswith(("-wal", "-shm"))
        links_ok = st.st_nlink == 1 or (is_sidecar and st.st_nlink == 0)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or not links_ok or st.st_mode & 0o077:
            raise sqlite3.DatabaseError(f"unsafe inbox file: {path.name}")


def _inbox_connect_locked():
    """Open, validate, and transactionally migrate private durable inbox database."""
    _preflight_state_dir(create=True)
    _preflight_regular_private(INBOX_DB, allow_missing=True)
    if not INBOX_DB.exists():
        fd = os.open(INBOX_DB, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    _secure_inbox_files()
    db = sqlite3.connect(INBOX_DB, timeout=30, isolation_level=None)
    try:
        check = db.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("inbox integrity check failed")
        _validate_schema_readonly(db)
        _secure_inbox_files()
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        _secure_inbox_files()
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
        extras = db.execute(
            "SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('view','index','trigger')"
        ).fetchall()
        if extras:
            raise sqlite3.DatabaseError("inbox schema contains unknown views, indexes, or triggers")
        if version > INBOX_SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"inbox schema version {version} is newer than supported")
        if not tables:
            if version != 0:
                raise sqlite3.DatabaseError("inbox schema version set but schema is empty")
            _create_inbox_table(db)
            _create_cleanup_table(db)
            _create_manifest_table(db)
        elif version == 0 and tables == {"inbound_delivery"}:
            # Exact known prototype layouts: original payload-free form and its payload extension.
            prototype_sql = _inbox_schema_sql(db)
            if prototype_sql == _schema_normalize(_inbox_ddl("{}")):
                payload_expr = "message_json"
            elif prototype_sql == _schema_normalize(_inbox_ddl(None)):
                payload_expr = "'{}'"
            else:
                raise sqlite3.DatabaseError("unknown or partial inbox prototype schema")
            cols = [(r[1], r[2].upper(), r[3], r[5]) for r in db.execute("PRAGMA table_info(inbound_delivery)")]
            names = [c[0] for c in cols]
            required = [
                "chat_uid",
                "message_uid",
                "order_ts",
                "order_uid",
                "delivery_id",
                "attempt_id",
                "attempt_count",
                "state",
                "last_error",
                "created_at",
                "updated_at",
            ]
            if names not in (required, ["chat_uid", "message_uid", "message_json"] + required[2:]):
                raise sqlite3.DatabaseError("unknown or partial inbox prototype schema")
            if "delivery_id" not in names or cols[0][3] != 1 or cols[1][3] != 2:
                raise sqlite3.DatabaseError("invalid inbox prototype keys")
            db.execute("ALTER TABLE inbound_delivery RENAME TO inbound_delivery_prototype")
            _create_inbox_table(db)
            db.execute(f"""INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,attempt_id,attempt_count,state,last_error,created_at,updated_at)
                SELECT chat_uid,message_uid,{payload_expr},order_ts,order_uid,delivery_id,attempt_id,attempt_count,state,last_error,created_at,updated_at FROM inbound_delivery_prototype""")
            db.execute("DROP TABLE inbound_delivery_prototype")
            _create_cleanup_table(db)
            _create_manifest_table(db)
            _migrate_accepted_cleanup(db)
            _migrate_v3_attachments(db)
        elif version == 2 and tables == {"inbound_delivery"}:
            if _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl()):
                raise sqlite3.DatabaseError("inbox schema signature mismatch")
            _create_cleanup_table(db)
            _create_manifest_table(db)
            _migrate_accepted_cleanup(db)
            _migrate_v3_attachments(db)
        elif version == 1 and tables == {"inbound_delivery"}:
            if _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl()):
                raise sqlite3.DatabaseError("inbox schema signature mismatch")
            _create_cleanup_table(db)
            _create_manifest_table(db)
            _migrate_accepted_cleanup(db)
            _migrate_v3_attachments(db)
        elif version == 3 and tables == {"inbound_delivery", "attachment_cleanup"}:
            if _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl()) or _cleanup_schema_sql(db) != _schema_normalize(
                _cleanup_ddl()
            ):
                raise sqlite3.DatabaseError("inbox schema signature mismatch")
            _create_manifest_table(db)
            _migrate_v3_attachments(db)
        elif version == INBOX_SCHEMA_VERSION and tables == {
            "inbound_delivery",
            "attachment_cleanup",
            "attachment_manifest",
        }:
            if (
                _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl())
                or _cleanup_schema_sql(db) != _schema_normalize(_cleanup_ddl())
                or _manifest_schema_sql(db) != _schema_normalize(_manifest_ddl())
            ):
                raise sqlite3.DatabaseError("inbox schema signature mismatch")
        else:
            raise sqlite3.DatabaseError("unsupported inbox schema")
        db.execute(f"PRAGMA user_version={INBOX_SCHEMA_VERSION}")
        db.execute("COMMIT")
        if version != INBOX_SCHEMA_VERSION:
            _checkpoint_wal_best_effort(db)
        _replay_attachment_cleanup(db)
        return db
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()
        raise


def inbox_connect():
    # WAL mode selection is a database-wide lock and SQLite may return SQLITE_BUSY
    # immediately when simultaneous first opens race. Serialize bootstrap locally.
    with INBOX_OPEN_LOCK:
        _preflight_state_dir(create=True)
        lock_path = STATE_DIR / "inbox-schema.lock"
        _preflight_regular_private(lock_path, allow_missing=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid() or lock_stat.st_nlink != 1:
                raise sqlite3.DatabaseError("unsafe inbox schema lock file")
            if lock_stat.st_mode & 0o077:
                raise sqlite3.DatabaseError("inbox schema lock permissions are too broad")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return _inbox_connect_locked()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _preflight_state_dir(create=False):
    if create:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = STATE_DIR.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise sqlite3.DatabaseError("unsafe inbox state directory")
    if st.st_mode & 0o077:
        raise sqlite3.DatabaseError("inbox state directory permissions are too broad")


def _preflight_regular_private(path, allow_missing=False):
    try:
        st = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
        raise sqlite3.DatabaseError(f"unsafe inbox file: {path.name}")
    if st.st_mode & 0o077:
        raise sqlite3.DatabaseError(f"inbox file permissions are too broad: {path.name}")


def _validate_schema_readonly(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > INBOX_SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"inbox schema version {version} is newer than supported")
    tables = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    extras = db.execute(
        "SELECT 1 FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('view','index','trigger') LIMIT 1"
    ).fetchone()
    if extras:
        raise sqlite3.DatabaseError("unknown inbox schema objects")
    if not tables and version == 0:
        return
    if version == INBOX_SCHEMA_VERSION:
        if tables != {"inbound_delivery", "attachment_cleanup", "attachment_manifest"}:
            raise sqlite3.DatabaseError("unknown inbox schema")
        if (
            _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl())
            or _cleanup_schema_sql(db) != _schema_normalize(_cleanup_ddl())
            or _manifest_schema_sql(db) != _schema_normalize(_manifest_ddl())
        ):
            raise sqlite3.DatabaseError("inbox schema signature mismatch")
        return
    if version == 3 and tables == {"inbound_delivery", "attachment_cleanup"}:
        if _inbox_schema_sql(db) != _schema_normalize(_inbox_ddl()) or _cleanup_schema_sql(db) != _schema_normalize(
            _cleanup_ddl()
        ):
            raise sqlite3.DatabaseError("inbox schema signature mismatch")
        return
    if tables != {"inbound_delivery"}:
        raise sqlite3.DatabaseError("unknown inbox schema")
    schema = _inbox_schema_sql(db)
    if version in (1, 2, 3):
        if schema != _schema_normalize(_inbox_ddl()):
            raise sqlite3.DatabaseError("inbox schema signature mismatch")
        return
    if version != 0 or schema not in (_schema_normalize(_inbox_ddl("{}")), _schema_normalize(_inbox_ddl(None))):
        raise sqlite3.DatabaseError("unknown or newer inbox schema")
    cols = [r[1] for r in db.execute("PRAGMA table_info(inbound_delivery)")]
    required = [
        "chat_uid",
        "message_uid",
        "order_ts",
        "order_uid",
        "delivery_id",
        "attempt_id",
        "attempt_count",
        "state",
        "last_error",
        "created_at",
        "updated_at",
    ]
    if cols not in (required, ["chat_uid", "message_uid", "message_json"] + required[2:]):
        raise sqlite3.DatabaseError("unknown inbox prototype columns")


def inbox_prepare(chat_uid: str, message_uid: str, key: list, message: dict):
    """Durably stage a message before side effect; return stable delivery ID and prior state."""
    db = inbox_connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT delivery_id,state,order_ts,order_uid FROM inbound_delivery WHERE chat_uid=? AND message_uid=?",
            (chat_uid, message_uid),
        ).fetchone()
        if row is None:
            delivery_id = uuid.uuid4().hex
            now = time.time()
            saved_message = dict(message)
            saved_message.setdefault("chat_uid", chat_uid)
            db.execute(
                "INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (
                    chat_uid,
                    message_uid,
                    json.dumps(saved_message, ensure_ascii=False),
                    key[0],
                    key[1],
                    delivery_id,
                    now,
                    now,
                ),
            )
            row = (delivery_id, "pending", key[0], key[1])
        else:
            saved_message = dict(message)
            saved_message.setdefault("chat_uid", chat_uid)
            # An earlier development schema tracked state without retaining payloads. Upgrade
            # retryable rows when their message is present on Plow's current page.
            db.execute(
                "UPDATE inbound_delivery SET message_json=? WHERE chat_uid=? AND message_uid=? "
                "AND message_json IN ('{}','') AND state!='handoff_accepted'",
                (json.dumps(saved_message, ensure_ascii=False), chat_uid, message_uid),
            )
            # A persisted dispatching row means process died or lost its acknowledgment. Do not
            # call it successful: retry with the same stable delivery ID, accepting duplicate risk.
            if row[1] == "dispatching":
                db.execute(
                    "UPDATE inbound_delivery SET state='ambiguous', last_error=?, updated_at=? "
                    "WHERE chat_uid=? AND message_uid=?",
                    ("previous process ended during dispatch; outcome unknown", time.time(), chat_uid, message_uid),
                )
                row = (row[0], "ambiguous", row[2], row[3])
        older = db.execute(
            "SELECT message_uid,state FROM inbound_delivery WHERE chat_uid=? "
            "AND (order_ts < ? OR (order_ts = ? AND order_uid < ?)) "
            "AND state != 'handoff_accepted' ORDER BY order_ts,order_uid LIMIT 1",
            (chat_uid, key[0], key[0], key[1]),
        ).fetchone()
        db.execute("COMMIT")
        return row[0], row[1], older
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def inbox_begin_attempt(chat_uid: str, message_uid: str) -> tuple:
    attempt_id = uuid.uuid4().hex
    db = inbox_connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT delivery_id,state,attempt_count FROM inbound_delivery WHERE chat_uid=? AND message_uid=?",
            (chat_uid, message_uid),
        ).fetchone()
        if row is None:
            raise sqlite3.OperationalError("inbound delivery row disappeared")
        if row[1] == "handoff_accepted":
            db.execute("COMMIT")
            return row[0], "", row[2], row[1]
        db.execute(
            "UPDATE inbound_delivery SET state='dispatching',attempt_id=?,attempt_count=attempt_count+1,"
            "last_error='',updated_at=? WHERE chat_uid=? AND message_uid=?",
            (attempt_id, time.time(), chat_uid, message_uid),
        )
        db.execute("COMMIT")
        return row[0], attempt_id, row[2] + 1, "dispatching"
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _legacy_attachment_name(item):
    uid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(item.get("uid") or "att"))
    filename = os.path.basename(item.get("filename") or "file")
    return f"{uid}-{filename}"


def _scoped_attachment_name(chat_uid, message_uid, attachment_uid, filename):
    scope = "\0".join((str(chat_uid), str(message_uid), str(attachment_uid))).encode()
    digest = hashlib.sha256(scope).hexdigest()
    suffix = Path(os.path.basename(filename or "file")).suffix
    suffix = suffix if suffix[1:].isalnum() and len(suffix) <= 16 else ""
    return f"m-{digest}{suffix}"


def _attachment_names_for(db, chat_uid, message_uid):
    return {
        row[0]
        for row in db.execute(
            "SELECT path FROM attachment_manifest WHERE chat_uid=? AND message_uid=?", (chat_uid, message_uid)
        ).fetchall()
    }


def _record_attachment(db, chat_uid, message_uid, attachment_uid, path):
    name = os.path.basename(path)
    existing = db.execute(
        "SELECT path FROM attachment_manifest WHERE chat_uid=? AND message_uid=? AND attachment_uid=?",
        (str(chat_uid), str(message_uid), str(attachment_uid)),
    ).fetchone()
    if existing:
        if existing[0] != name:
            raise ValueError("attachment manifest conflict")
        return
    db.execute(
        "INSERT INTO attachment_manifest(chat_uid,message_uid,attachment_uid,path) VALUES(?,?,?,?)",
        (str(chat_uid), str(message_uid), str(attachment_uid), name),
    )


def _attachment_manifest_matches(chat_uid, message_uid, atts):
    db = inbox_connect()
    try:
        for item in atts:
            uid = str(item.get("uid") or "att")
            expected = _scoped_attachment_name(chat_uid, message_uid, uid, item.get("filename") or "file")
            row = db.execute(
                "SELECT path FROM attachment_manifest WHERE chat_uid=? AND message_uid=? AND attachment_uid=?",
                (str(chat_uid), str(message_uid), uid),
            ).fetchone()
            if row and row[0] != expected:
                return False
        return True
    finally:
        db.close()


def _validate_attachment_ids(atts):
    ids = [str(item.get("uid") or "att") for item in atts if isinstance(item, dict)]
    if len(ids) != len(atts) or len(ids) != len(set(ids)):
        raise ValueError("duplicate or invalid attachment uid")


def _preflight_attachment_dir(create=False):
    if create:
        ATTACHMENTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = ATTACHMENTS.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OSError("unsafe private attachment directory")


def _queue_attachment_cleanup(db, chat_uid, message_uid):
    now = time.time()
    for name in _attachment_names_for(db, chat_uid, message_uid):
        # Persist only a direct child filename; never accept a stored path.
        if name not in (".", "..") and "/" not in name and "\\" not in name:
            db.execute("INSERT OR IGNORE INTO attachment_cleanup(path,created_at) VALUES(?,?)", (name, now))


def _replay_attachment_cleanup(db):
    try:
        rows = db.execute("SELECT path FROM attachment_cleanup ORDER BY created_at").fetchall()
    except sqlite3.Error:
        return
    for (name,) in rows:
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or os.path.basename(name) != name
        ):
            continue
        dfd = None
        try:
            _preflight_attachment_dir(create=False)
            dfd = os.open(ATTACHMENTS, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            dst = os.fstat(dfd)
            if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != os.getuid() or dst.st_mode & 0o077:
                raise OSError("unsafe private attachment directory")
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None:
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                    continue
                os.unlink(name, dir_fd=dfd)
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attachment_cleanup WHERE path=?", (name,))
            db.execute("DELETE FROM attachment_manifest WHERE path=?", (name,))
            db.execute("COMMIT")
        except FileNotFoundError:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attachment_cleanup WHERE path=?", (name,))
            db.execute("DELETE FROM attachment_manifest WHERE path=?", (name,))
            db.execute("COMMIT")
        except OSError as exc:
            if db.in_transaction:
                db.execute("ROLLBACK")
            log(f"attachment cleanup deferred ({type(exc).__name__})")
        finally:
            if dfd is not None:
                os.close(dfd)


def inbox_finish(chat_uid: str, message_uid: str, state: str, error: str = ""):
    db = inbox_connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        if state == "handoff_accepted":
            _queue_attachment_cleanup(db, chat_uid, message_uid)
        # One transaction changes durable acceptance and erases serialized content.
        # Stable IDs/order/state remain as a dedupe and high-water tombstone.
        cur = db.execute(
            "UPDATE inbound_delivery SET state=?,message_json=CASE WHEN ?='handoff_accepted' THEN '' ELSE message_json END,last_error=?,updated_at=? "
            "WHERE chat_uid=? AND message_uid=?",
            (state, state, error[:500], time.time(), chat_uid, message_uid),
        )
        if cur.rowcount != 1:
            raise sqlite3.OperationalError("inbound delivery row disappeared before acknowledgment")
        db.execute("COMMIT")
        if state == "handoff_accepted":
            _replay_attachment_cleanup(db)
        if state == "handoff_accepted":
            _checkpoint_wal_best_effort(db)
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _checkpoint_wal_best_effort(db):
    # Other readers and filesystem snapshots can retain old pages despite this attempt.
    try:
        db.execute("PRAGMA busy_timeout=0")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def inbox_status(chat_uid: str, message_uid: str):
    db = inbox_connect()
    try:
        return db.execute(
            "SELECT delivery_id,attempt_id,attempt_count,state,last_error FROM inbound_delivery "
            "WHERE chat_uid=? AND message_uid=?",
            (chat_uid, message_uid),
        ).fetchone()
    finally:
        db.close()


def inbox_pending_messages(chat_uid: str) -> list:
    """Return durable unaccepted events so recovery does not depend on API page retention."""
    db = inbox_connect()
    try:
        rows = db.execute(
            "SELECT message_uid,message_json,created_at,state FROM inbound_delivery WHERE chat_uid=? "
            "AND state!='handoff_accepted' ORDER BY order_ts,order_uid",
            (chat_uid,),
        ).fetchall()
        pending = []
        for message_uid, payload, created_at, state in rows:
            age = max(0, int(time.time() - created_at))
            if age >= 86400:
                ident = (chat_uid, message_uid)
                if ident not in STALE_ALERTED:
                    result = send_to_agent(
                        f"MyPlow privacy alert: unresolved inbound message older than 24h; chat={chat_uid}; message={message_uid}; state={state}. No content included. Resolve explicitly: {shlex.quote(sys.executable)} {shlex.quote(SELF)} resolve-pending {shlex.quote(chat_uid)} {shlex.quote(message_uid)} retry|discard"
                    )
                    if result:
                        STALE_ALERTED.add(ident)
            if not payload or payload == "{}":
                continue
            try:
                message = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict) and message.get("uid"):
                pending.append(message)
        return pending
    finally:
        db.close()


def inbox_resolve_pending(chat_uid: str, message_uid: str, action: str):
    if action not in ("retry", "discard"):
        raise ValueError("action must be retry or discard")
    db = inbox_connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT message_json,state FROM inbound_delivery WHERE chat_uid=? AND message_uid=?",
            (chat_uid, message_uid),
        ).fetchone()
        if row is None or row[1] == "handoff_accepted":
            raise ValueError("no unresolved pending row")
        if action == "retry":
            db.execute(
                "UPDATE inbound_delivery SET state='pending',last_error='explicitly resolved for retry',updated_at=? WHERE chat_uid=? AND message_uid=?",
                (time.time(), chat_uid, message_uid),
            )
        else:
            try:
                _queue_attachment_cleanup(db, chat_uid, message_uid)
            except (TypeError, json.JSONDecodeError, AttributeError):
                pass
            db.execute("DELETE FROM inbound_delivery WHERE chat_uid=? AND message_uid=?", (chat_uid, message_uid))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    if action == "discard":
        db = inbox_connect()
        try:
            _replay_attachment_cleanup(db)
        finally:
            db.close()


def advance_mark(chat_uid: str, key: list):
    """Advance legacy high-water mark only after persistent dispatch acceptance."""
    with STATE_LOCK:
        st = read_json(STATE, {})
        marks = st.setdefault("marks", {})
        current = marks.get(chat_uid)
        if current is None or key > current:
            marks[chat_uid] = key
            write_json(STATE, st)


def conversation(answer, message) -> tuple:
    """(who answered last, minutes before this text, what they said) while that answer is recent, else ()."""
    if not answer:
        return ()
    try:
        at = lambda m: datetime.fromisoformat((m.get("created_at") or "").replace("Z", "+00:00"))
        minutes = (at(message) - at(answer)).total_seconds() / 60
    except (ValueError, TypeError):
        minutes = None  # no readable times: who answered last still counts
    if minutes is not None and minutes > FOLLOW_MINUTES:
        return ()
    return signed_by(answer["body"]), minutes, answer["body"]


def route_message(message: dict, chat: str = "", last: tuple = ()) -> str:
    """Serialize per-chat processing, then route via the durable inbox/outbox."""
    chat_uid = message.get("chat_uid") or chat
    if not chat_uid:
        return _route_message_locked(message, chat, last)
    with route_lock(chat_uid):
        return _route_message_locked(message, chat_uid, last)


def _route_message_locked(message: dict, chat: str = "", last: tuple = ()) -> str:
    """Durably hand off one inbound message; crash recovery is at-least-once, not exactly-once."""
    if message.get("direction") != "inbound":
        return "skip: outbound"
    mid = message.get("uid") or ""
    text = (message.get("body") or "").strip()
    atts = message.get("attachments") or []
    # A photo with no caption is a message too: skipping it silently read as the
    # Boss ignoring the owner.
    if not mid or not (text or atts):
        return "skip: empty"

    try:
        _validate_attachment_ids(atts)
    except ValueError:
        log("attachment envelope rejected (duplicate or invalid uid)")
        return "ERROR invalid attachment identifiers"

    chat_uid = message.get("chat_uid") or chat
    chat = chat or chat_uid
    try:
        if atts and not _attachment_manifest_matches(chat, mid, atts):
            log("attachment envelope rejected (manifest conflict)")
            return "ERROR attachment manifest conflict"
    except sqlite3.Error as e:
        log(f"ERROR attachment manifest ({type(e).__name__})")
        return "ERROR attachment manifest unavailable"
    key = order(message)
    # Keep existing high-water marks as a compatibility floor, but never advance them before
    # send acceptance. An inbox row is the durable per-message claim and is safe to replay.
    try:
        existing = inbox_status(chat, mid)
        with STATE_LOCK:
            prev = read_json(STATE, {}).get("marks", {}).get(chat)
        if existing is None and prev is not None and key <= prev:
            return "skip: already routed"
        delivery_id, delivery_state, older = inbox_prepare(chat, mid, key, message)
    except sqlite3.Error as e:
        log(f"ERROR inbox persistence ({type(e).__name__})")
        return "ERROR inbox persistence failed; retrying next pass"
    if delivery_state == "handoff_accepted":
        try:
            advance_mark(chat, key)
        except OSError as e:
            log(f"ERROR high-water mark ({type(e).__name__}); accepted tombstone remains")
            return "ERROR high-water mark not recorded; retrying next pass"
        return "skip: already routed"
    if older:
        log(f"inbox blocked out-of-order message; older state={older[1]}")
        return "ERROR earlier chat message still pending; retrying next pass"

    who = speaker(message.get("sender") or {})
    files, notes = [], []  # a file that did not arrive is a note, never "open it"
    for a in atts:
        try:
            expected = _scoped_attachment_name(chat_uid, mid, str(a.get("uid") or "att"), a.get("filename") or "file")
            db = inbox_connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                _record_attachment(db, chat_uid, mid, a.get("uid") or "att", expected)
                db.execute("COMMIT")
            finally:
                db.close()
            files.append(download(a, chat_uid, mid))
        except (OSError, ValueError) as e:
            log(f"attachment download failed ({type(e).__name__})")
            notes.append(f"could not download {a.get('filename') or 'attachment'}: {e}")
            continue
        except sqlite3.Error as e:
            log(f"attachment manifest failed ({type(e).__name__})")
            notes.append(f"could not download {a.get('filename') or 'attachment'}: attachment manifest unavailable")
            continue
        if (a.get("content_type") or mimetypes.guess_type(files[-1])[0] or "").startswith("audio/"):
            try:
                text = f"{text}\n[voice note, transcribed] {transcribe(files[-1])}".strip()
            except Exception as e:  # noqa: BLE001 - a deaf STT must never lose the voice note
                log(f"voice note transcription failed ({type(e).__name__})")
                notes.append(f"voice note not transcribed ({e}): the transcript is missing, only the audio file came")
    with STATE_LOCK:
        taps = read_json(STATE, {}).get("pending_tapbacks", [])
    target = pick_agent(text, message, *last)
    try:
        delivery_id, attempt_id, attempt_count, attempt_state = inbox_begin_attempt(chat, mid)
    except sqlite3.Error as e:
        log(f"ERROR inbox attempt ({type(e).__name__})")
        return "ERROR inbox attempt failed; retrying next pass"
    if attempt_state == "handoff_accepted":
        advance_mark(chat, key)
        return "skip: already routed"
    try:
        sent = send_to_agent(
            envelope(
                who,
                text,
                chat_uid,
                files,
                notes + context(message) + taps,
                mid,
                target.rsplit(":", 1)[1],
                owner=is_owner(message.get("sender") or {}),
                delivery_id=delivery_id,
            ),
            target,
        )
    except Exception as e:
        # Treat unexpected transport exceptions as ambiguous; no high-water advance. BaseException
        # is deliberately not caught so a process-crash test leaves the durable dispatching row.
        sent = None
        log(f"ERROR mp send raised {type(e).__name__}; outcome unknown")
    if sent:
        try:
            inbox_finish(chat, mid, "handoff_accepted")
            advance_mark(chat, key)
        except (sqlite3.Error, OSError) as e:
            log(f"ERROR inbox acceptance ({type(e).__name__}); retry retains stable delivery")
            return "ERROR inbox acceptance not recorded; retrying next pass"
        if taps:
            with STATE_LOCK:
                st = read_json(STATE, {})
                st["pending_tapbacks"] = [t for t in st.get("pending_tapbacks", []) if t not in taps]
                write_json(STATE, st)
        return f"ROUTE chat={chat_uid} message={mid}"
    outcome = "refused" if sent is False else "ambiguous"
    try:
        inbox_finish(chat, mid, outcome, "mp send refused" if sent is False else "mp send outcome unknown")
    except sqlite3.Error as e:
        log(f"ERROR inbox result ({type(e).__name__}); retry retains stable delivery")
        return "ERROR inbox result not recorded; retrying next pass"
    with STATE_LOCK:
        st = read_json(STATE, {})
        warned = mid in st.get("warned_ids", [])
        late = min(message_age(message), time.time() - STARTED) >= WARN_AFTER
        # Only a refusal is a failure to tell them about. An mp send that did not finish may have reached
        # the Boss (11-08: Camily was told twice that her texts had not, and both had), so that text is
        # never warned about, then or later.
        if (late or sent is None) and not warned:
            st["warned_ids"] = (st.get("warned_ids", []) + [mid])[-200:]
        write_json(STATE, st)
    log(f"inbox {outcome} chat={chat} message={mid} attempt_count={attempt_count}")
    if not late or warned or sent is None:
        # A Boss that is coming up (a restart, an in-place update) gets it on the next pass; a text
        # that keeps failing is told once, not every pass.
        return "ERROR mp send failed; retrying next pass"
    # Say it on the phone that just texted, the one path known to work, in their language, instead
    # of leaving them waiting on an answer nobody heard.
    try:
        send_message(NOT_YET[language(text)], chat_uid)
    except (SystemExit, OSError) as exc:
        log(f"could not warn sender ({type(exc).__name__})")
    return "ERROR mp send failed; sender warned, retrying next pass"


def is_owner(sender: dict) -> bool:
    """Plow marks the account's owner. The WhatsApp lab marks everyone it allowlists, so a line's creds can name the one
    owner_handle; a mismatch only costs the owner the rule, never hands it to someone else."""
    handle = read_json(CREDS, {}).get("owner_handle")
    return sender.get("role") == "owner" and (not handle or sender.get("provider_key") == handle)


def load_creds() -> dict:
    """A saved login wins; otherwise a cloud agent's environment is the login."""
    creds = read_json(CREDS, {})
    if not creds.get("token") and os.environ.get("PLOW_API_BASE"):
        creds = {"token": os.environ.get("PLOW_AGENT_TOKEN") or "proxied", "chat_uid": "", "cloud": True}
    return creds


def download(att: dict, chat_uid: str = "", message_uid: str = "") -> str:
    """Save one inbound attachment where the Boss can open it; return its path."""
    attachment_uid = str(att.get("uid") or "att")
    name = os.path.basename(att.get("filename") or "file")
    dest = ATTACHMENTS / _scoped_attachment_name(chat_uid, message_uid, attachment_uid, name)
    _preflight_state_dir()
    ATTACHMENTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    _preflight_attachment_dir(create=False)
    dfd = os.open(ATTACHMENTS, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        dirst = os.fstat(dfd)
        if not stat.S_ISDIR(dirst.st_mode) or dirst.st_uid != os.getuid() or dirst.st_mode & 0o077:
            raise OSError("unsafe private attachment directory")
        leaf = dest.name
        try:
            lst = os.stat(leaf, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            lst = None
        if lst is not None:
            if (
                not stat.S_ISREG(lst.st_mode)
                or lst.st_uid != os.getuid()
                or lst.st_nlink != 1
                or stat.S_IMODE(lst.st_mode) != 0o600
            ):
                raise OSError("unsafe existing attachment")
            fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
            try:
                opened = os.fstat(fd)
                if (
                    (opened.st_dev, opened.st_ino) != (lst.st_dev, lst.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != 0o600
                ):
                    raise OSError("unsafe existing attachment")
            finally:
                os.close(fd)
            return str(dest)
    finally:
        os.close(dfd)
    if not att.get("url"):  # failed media comes with no url: there is nothing to fetch
        raise ValueError("file unavailable")
    req = urllib.request.Request(BASE + att["url"])  # a signed, short-lived path on the chat
    token = load_creds().get("token")
    if token:
        # Not forwarded on a redirect: a signed storage URL refuses a second credential.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        dfd = os.open(ATTACHMENTS, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(
                dest.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd
            )
            with os.fdopen(fd, "wb") as out:
                out.write(r.read())
        finally:
            os.close(dfd)
    return str(dest)


def transcribe(path: str) -> str:
    """What a saved voice note says, from the STT server. Raises when there is none, it fails, or it heard nothing."""
    if not STT_URL:
        raise OSError("no STT server set (PLOW_CHAT_STT_URL)")
    suffix = Path(path).suffix if Path(path).suffix[1:].isalnum() else ""  # the server reads the format from it
    edge = os.urandom(16).hex()
    data = (
        (
            f'--{edge}\r\nContent-Disposition: form-data; name="audio"; filename="voice{suffix}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + Path(path).read_bytes()
        + f"\r\n--{edge}--\r\n".encode()
    )
    # To the name's IPv4 address when it has one, the name kept as Host (a proxy routes on it): a VPN exit node can take
    # the IPv6 one, and Daniel's first voice note on Mac 1 died on "No route to host" for server.local (10-20).
    u, url = urllib.parse.urlsplit(STT_URL), STT_URL
    if u.scheme == "http":  # https checks the certificate against the name in the URL
        try:
            ip = socket.getaddrinfo(u.hostname, u.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            url = u._replace(netloc=f"{ip}:{u.port}" if u.port else ip).geturl()
        except OSError:
            pass  # no IPv4 address: the name as given
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Host": u.netloc, "Content-Type": f"multipart/form-data; boundary={edge}"},
    )
    with urllib.request.urlopen(req, timeout=STT_SECONDS) as r:
        heard = " ".join((json.load(r).get("text") or "").split())
    if not heard:
        raise ValueError("no words heard")
    return heard


def upload(path: str, chat_uid: str, creds: dict) -> str:
    """Declare one outbound file on the chat, PUT its bytes, return its attachment uid."""
    data = Path(path).read_bytes()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    status, a = api(
        "POST",
        f"/v1/chats/{chat_uid}/attachments",
        {"filename": os.path.basename(path), "content_type": ctype, "size_bytes": len(data)},
        token=creds["token"],
    )
    if status >= 400:
        raise SystemExit(f"attachment refused {status}: {json.dumps(a)[:200]}")
    req = urllib.request.Request(a["upload_url"], data=data, method="PUT", headers=a.get("upload_headers") or {})
    urllib.request.urlopen(req, timeout=300).close()
    return a["uid"]


# --- Outbound ---


def send_message(text: str, chat_uid: str = "", creds=None, files=()) -> dict:
    creds = creds or load_creds()
    if not creds.get("token"):
        raise SystemExit("no Plow Chat credentials yet: activation is not done")
    # No thread named = the owner's own 1:1: the chat activation resolved, or
    # in the cloud the first chat on this agent's line.
    chat_uid = chat_uid or creds["chat_uid"] or next(iter(list_chats(creds)), "")
    body = {"body": text}
    if files:
        body["attachment_uids"] = [upload(f, chat_uid, creds) for f in files]
    status, data = api("POST", f"/v1/chats/{chat_uid}/messages", body, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"send failed {status}: {json.dumps(data)[:200]}")
    return data


def react(chat_uid: str, message_uid: str, kind: str, creds=None) -> dict:
    """Tapback one message (a TAPBACKS name or one emoji), or `remove` this line's tapback."""
    if kind == "remove":
        body = {"operation": "remove"}
    elif kind in TAPBACKS:
        body = {"operation": "add", "type": kind}
    elif kind and not kind.isascii() and len(kind) <= 8:  # an emoji, not a misspelt name
        body = {"operation": "add", "type": "custom", "custom_emoji": kind}
    else:
        body = None
    if not (body and chat_uid.startswith("cht_") and message_uid.startswith("msg_")):
        raise SystemExit(f"usage: {SELF} react cht_x msg_y {'|'.join(TAPBACKS)}|EMOJI|remove")
    creds = creds or load_creds()
    status, data = api("POST", f"/v1/chats/{chat_uid}/messages/{message_uid}/reactions", body, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"tapback failed {status}: {json.dumps(data)[:200]}")
    return data


# --- Activation ---


def mint_activation() -> dict:
    status, data = api("POST", "/v1/auth/activate", {"name": "MyPeople Boss", "provision_chat": False})
    if status >= 400 or "activation_secret" not in data:
        raise RuntimeError(f"activate failed {status}: {json.dumps(data)[:200]}")
    write_json(ACTIVATION, data)
    log("activation code generated; details kept out of logs")
    return data


def last_message_ts(token: str, chat_uid: str) -> str:
    _, data = api("GET", f"/v1/chats/{chat_uid}/messages", token=token)
    return max((m.get("created_at") or "" for m in listing(data)), default="")


def resolve_chat_uid(token: str, redeem: dict, last_ts=last_message_ts) -> str:
    """The chat this account actually talks in.

    An account keeps every chat a past activation provisioned, most of them
    dead, so pick by the newest message rather than the first listed.
    """
    chat = redeem.get("chat") or {}
    if chat.get("uid"):
        return chat["uid"]
    _, data = api("GET", "/v1/chats", token=token)
    chats = listing(data)
    live = [c for c in chats if c.get("status") == "active" and c.get("uid")] or chats
    if not live:
        raise RuntimeError(f"no chat on this account: {json.dumps(data)[:200]}")
    return max(live, key=lambda c: last_ts(token, c["uid"]))["uid"]


def activation_loop() -> dict:
    """Hold a live code until the owner texts it; codes expire, so re-mint."""
    a = read_json(ACTIVATION, {})
    if not a.get("activation_secret"):
        a = mint_activation()
    while True:
        status, data = api("POST", "/v1/auth/activate/redeem", {"activation_secret": a["activation_secret"]})
        if status == 410 or data.get("status") == "expired":
            log("activation code expired, minting a fresh one")
            a = mint_activation()
        elif data.get("status") == "verified" and data.get("token"):
            token = data["token"]
            creds = {"token": token, "chat_uid": resolve_chat_uid(token, data)}
            write_json(CREDS, creds)
            ACTIVATION.unlink(missing_ok=True)
            log(f"activated: chat {creds['chat_uid']}")
            return creds
        time.sleep(5)


# --- Bridge ---


def list_chats(creds: dict) -> list:
    if creds.get("cloud"):
        # Live read every pass: a group the line joins later is a new chat. Not the legacy agents/cloud/me:
        # Plow keeps that legacy path only until the images move off it (plow api/plow/main.py).
        status, data = api("GET", "/v1/agents/me", token=creds["token"])
        if status >= 400:
            log(f"/me failed HTTP {status}")
            return []
        return [c["uid"] for c in data.get("chats") or [] if c.get("uid")]
    status, data = api("GET", "/v1/chats", token=creds["token"])
    if status >= 400:
        # A listing hiccup must not cost the owner their own 1:1.
        log(f"chat listing failed HTTP {status}")
        return [creds["chat_uid"]]
    return [c["uid"] for c in listing(data) if c.get("uid")] or [creds["chat_uid"]]


def poll_chat(creds: dict, chat_uid: str, seed: bool = True) -> int:
    """Route new messages in one chat, seeding it silently the first time.

    Returns how many it routed, or -1 while one still waits (the read or the Boss failed).
    Seeding is per chat: a group joined months in carries its own history, and
    replaying it would hand the Boss a fake inbox. Only chats already there when
    the bridge starts are seeded; one that appears while it runs was started by
    a text, and swallowing that text would leave the sender unanswered.
    """
    # The literal text: Plow's default body is markdown, so an owner's "jane_doe" reached agents as
    # "jane\_doe" (10-29). Sends are already verbatim.
    status, data = api("GET", f"/v1/chats/{chat_uid}/messages?format=text_decorations", token=creds["token"])
    if status >= 400:
        log(f"chat read failed HTTP {status}")
        return -1
    msgs = listing(data)
    with STATE_LOCK:
        st = read_json(STATE, {})
        seeded = set(st.get("seeded_chats", []))
        first = chat_uid not in seeded
        marks = st.setdefault("marks", {})
        if first and not seed:
            # The floor mark routes everything on its page, and gives it a mark at once. Without
            # one, a chat whose first page was only our own send (Plow's send-first POST
            # /v1/chats) passed next time for a chat from before marks, which marks its whole
            # page handled, and its first reply never reached the Boss.
            st["seeded_chats"] = sorted(seeded | {chat_uid})
            marks[chat_uid] = FLOOR
            write_json(STATE, st)
            first = False
        mark = None
        if first:
            mark = max((order(m) for m in msgs), default=FLOOR)  # empty at startup: same floor
        elif chat_uid in seeded and chat_uid not in marks and msgs:
            # Seen before marks existed: the mark is the newest message the old id list says was
            # routed, or, if they all aged out of it, the newest on the page (no replay).
            legacy = set(st.get("seen_ids", []))
            done = [order(m) for m in msgs if m.get("uid") in legacy]
            mark = max(done or [order(m) for m in msgs])
        if mark or first:
            if mark:
                marks[chat_uid] = mark
            if first:
                st["seeded_chats"] = sorted(seeded | {chat_uid})
            write_json(STATE, st)
    note_tapbacks(msgs, chat_uid, seed)
    if first:
        log(f"seeded {len(msgs)} existing message(s) in {chat_uid} as already handled")
        if creds.get("cloud") and not creds.get("owner_handle"):
            # A fresh cloud agent's first text was the signup phrase, which the
            # seed just swallowed; without this the new owner hears nothing.
            # A MyPlow routed to its person by Build an Agent (route sets owner_handle) greets them itself, by
            # name and about their ask (first-text.py): a generic hello first reads as a second, worse greeting
            # (Daniel 10-28).
            try:
                send_message(WELCOME, chat_uid, creds)
            except SystemExit as e:  # a failed hello must not take the bridge down
                log(f"welcome not sent ({type(e).__name__})")
        return 0
    routed, last = 0, None
    # A pending row may have aged out of Plow's paginated history. Merge the durable payloads
    # back into this pass before routing so an older failure cannot block all newer messages.
    by_uid = {m.get("uid"): m for m in msgs if m.get("uid")}
    for m in inbox_pending_messages(chat_uid):
        by_uid.setdefault(m.get("uid"), m)
    for m in sorted(by_uid.values(), key=order):
        if m.get("direction") == "outbound" and signed_by(m.get("body")):
            last = m  # who answered last, for the router
        r = route_message(m, chat_uid, conversation(last, m))
        if not r.startswith("skip"):
            log(r)
        if r.startswith("ERROR"):
            return -1  # routing a newer one would move the mark past this one, and it would be lost
        routed += r.startswith("ROUTE")
    return routed


def sweep(creds: dict, seed: bool = False) -> bool:
    """One pass over every chat; False while a text still waits on the Boss."""
    return min([poll_chat(creds, uid, seed) for uid in list_chats(creds)], default=0) >= 0


def open_socket(creds: dict) -> WebSocket:
    # No chat_id: an account ticket carries every chat, a group made after it connected included.
    status, data = api("POST", "/v1/ws/ticket", {}, token=creds["token"])
    if status >= 400 or not data.get("ticket"):
        raise OSError(f"ws ticket {status}: {json.dumps(data)[:160]}")
    ws = WebSocket(f"{BASE.replace('http', 'ws', 1)}/v1/ws?ticket={urllib.parse.quote(data['ticket'])}")
    # The server speaks first with {"type": "connected"}: a ticket it refuses is accepted, then closed with 4401.
    first = ws.recv(15)
    if json.loads(first or "{}").get("type") != "connected":
        raise OSError(f"websocket opened without connected: {first!r:.100}")
    return ws


def listen(creds: dict, ws: WebSocket, due: float):
    """Read each chat the moment a frame says it changed, until the socket dies (it raises).

    A frame only names the chat: the read routes through the same marks as a pass, so the text of
    a frame the server dropped still arrives, in order, with the next frame or pass.
    """
    ping = time.time() + PING_SECONDS
    while True:
        raw = ws.recv(max(0.05, min(due, ping) - time.time()))
        if raw is not None:
            event = json.loads(raw)
            chat = event.get("chat_id")
            # Delivery receipts of the Boss's own replies change nothing to route.
            if chat and event.get("event_type") != "message_status_updated" and poll_chat(creds, chat, False) < 0:
                due = min(due, time.time() + POLL_SECONDS)
        now = time.time()
        if now >= due:
            due = now + (SWEEP_SECONDS if sweep(creds) else POLL_SECONDS)
        if now >= ping:
            if now - ws.heard > 2 * PING_SECONDS:
                raise OSError(f"websocket silent for {now - ws.heard:.0f}s")
            ws.send(b"", 0x9)
            ping = now + PING_SECONDS


def bridge_loop(creds: dict):
    log(f"bridge up, routing to {BOSS_AGENT}")
    seed, fails, retry_at = True, 0, 0.0
    while True:
        ws = None
        if time.time() >= retry_at:
            try:
                ws = open_socket(creds)
                fails = 0
                log("websocket connected")
            except (OSError, ValueError) as e:
                fails += 1
                retry_at = time.time() + min(POLL_SECONDS * 2**fails, 300)
                log(f"websocket down ({type(e).__name__}); polling every {POLL_SECONDS:g}s")
        try:
            # After connecting, so a text sent during this catch-up also comes as a frame (the marks
            # make that second sight a no-op). Without a socket, this pass is the whole bridge.
            ok = sweep(creds, seed)
            seed = False
            if ws:
                listen(creds, ws, time.time() + (SWEEP_SECONDS if ok else POLL_SECONDS))
        except (OSError, ValueError) as e:
            log(f"websocket lost ({type(e).__name__})" if ws else f"poll error ({type(e).__name__})")
        finally:
            if ws:
                ws.sock.close()
        time.sleep(1 if ws else POLL_SECONDS)


HELP = ("-h", "--help")


def main():
    # No default: a bare run started a second, unsupervised serve beside supervise.sh's (discord-chat, Mac 2, 10-30).
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    # An option is never message text: two engineers' `reply --help` reached the owner's chat as
    # the literal text "--help" (10-05). -h/--help on any subcommand prints this and does nothing
    # else (on `serve` it used to start a second bridge), and reply/react refuse any other leading
    # "--" word instead of sending it.
    if cmd in HELP or (args and args[0] in HELP):
        print(__doc__.strip())
        return
    if cmd == "message":  # the board's Plugins page shows this
        print(TEMPLATE)
        return
    if cmd == "resolve-pending":
        if len(args) != 3:
            raise SystemExit(f"usage: {SELF} resolve-pending CHAT_UID MESSAGE_UID retry|discard")
        inbox_resolve_pending(*args)
        print("pending row resolved")
        return
    if cmd == "reply":
        chat_uid = args.pop(0) if args and args[0].startswith("cht_") else ""
        files = []
        while args and (args[0] in HELP or args[0].startswith("--")):
            if args[0] in HELP:
                print(__doc__.strip())
                return
            if args[0] != "--file" or len(args) < 2:
                raise SystemExit(
                    f"{args[0]!r} is not an option of reply, nothing sent. "
                    f'usage: {SELF} reply [cht_x] [--file PATH ...] "text"'
                )
            files.append(args[1])
            args = args[2:]
        if not args and not files:
            raise SystemExit(f'usage: {SELF} reply [cht_x] [--file PATH ...] "text"')
        print(json.dumps(send_message(" ".join(args), chat_uid, files=files)))
    elif cmd == "react":
        flag = next((a for a in args if a in HELP or a.startswith("--")), None)
        if flag in HELP:
            print(__doc__.strip())
            return
        if flag:
            raise SystemExit(
                f"{flag!r} is not an option of react, nothing sent. "
                f"usage: {SELF} react cht_x msg_y like|love|...|EMOJI|remove"
            )
        args = args + ["", "", ""]
        print(json.dumps(react(*args[:3])))
    elif cmd == "api":
        if len(args) < 2 or not args[1].startswith("/v1/"):
            raise SystemExit(f"usage: {SELF} api GET|POST|PUT|PATCH|DELETE /v1/PATH ['JSON body']")
        status, data = api(
            args[0].upper(), args[1], json.loads(args[2]) if len(args) > 2 else None, token=load_creds().get("token")
        )
        print(json.dumps(data, indent=2))
        if status >= 400:
            raise SystemExit(f"HTTP {status}")
    elif cmd == "status":
        creds, act = load_creds(), read_json(ACTIVATION, {})
        print(
            json.dumps(
                {
                    "activated": bool(creds.get("token")),
                    "cloud": bool(creds.get("cloud")),
                    "chat_uid": creds.get("chat_uid"),
                    "text_this": f"Plow Activate: {act['display_code']}" if act.get("display_code") else None,
                    "to": act.get("send_to"),
                    "seeded_chats": read_json(STATE, {}).get("seeded_chats"),
                },
                indent=2,
            )
        )
    elif cmd == "serve":
        creds = load_creds()
        creds = creds if creds.get("token") else activation_loop()
        bridge_loop(creds)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
