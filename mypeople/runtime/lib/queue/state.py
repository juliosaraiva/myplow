"""MyPlow queue-server: HTTP queue + registry + reaper + /dashboard + /roster.
Binds BIND_ADDR:HUD_PORT (9900). Reverse-proxies TODO routes to TODO_PORT so both
front doors serve both pages (symmetric cross-nav, §ITEM-2). Python stdlib only.

Helpers above Handler own the roster file, per-agent status, and the reaper.
Handler serves /health, the HUD, /agents, /clients, /roster, /heartbeat, and
/task/*, and proxies board routes to the todo server.
"""

import os
import sys
import time
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
HUD_PORT = int(CFG["HUD_PORT"])
TODO_PORT = int(CFG["TODO_PORT"])
DEAD_AFTER = float(CFG["QUEUE_DEAD_AFTER"])
CLIENT_TTL = DEAD_AFTER  # stale-client expiry (~4-5 heartbeats)
ROSTER_PATH = os.path.join(INSTALL_DIR, "run", "roster.json")
STATUS_DIR = os.path.join(INSTALL_DIR, "status")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

START = time.time()
LOCK = threading.RLock()
CLIENTS = {}  # hostname -> record
AGENTS = {}  # agent_id -> record
TASKS = {}  # task_id -> record
# A client's attach_base is now operator-authoritative (set via TTYD_PUBLIC_URL; there is no
# auto-derivation). Only reject bind-any / empty addresses that can never be a usable client URL.
# Loopback (localhost/127.0.0.1) is honored — it is exactly right for the single-host,
# published-ports posture where the browser reaches ttyd on the same host.
NONROUTABLE = {"0.0.0.0", "127.0.0.1", "localhost", "::", ""}


def now():
    return time.time()


def load_roster():
    r = C.read_json(ROSTER_PATH, {}) or {}
    return r if isinstance(r, dict) else {}


def status_for(agent_id):
    _, sess, tab = C.parse_agent_id(agent_id)
    p = os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab)
    return C.read_json(p, None)


def display_status(raw):
    # idle, blocked, working and starting as written; missing->ready. "starting" = spawned and never
    # prompted: it sits at its prompt, so showing it as "working" made 7 fresh agents look busy for
    # 6 h and the Latch rollout skipped their homes (10-29).
    s = (raw or {}).get("status", "")
    return s if s in ("idle", "blocked", "working", "starting") else "ready"


def agent_row(aid, rec, roster=None):
    host = rec.get("host", "")
    client = CLIENTS.get(host, {})
    ab = client.get("attach_base", "") or ""
    # never advertise a non-routable attach_base
    from urllib.parse import urlparse

    try:
        h = urlparse(ab).hostname if ab else ""
    except Exception:
        h = ""
    if h in NONROUTABLE:
        ab = ""
    tgt = rec.get("tmux_target") or C.tmux_target(aid)
    attach_url = ("%s/?arg=-t&arg=%s" % (ab, tgt)) if ab else ""
    roster = load_roster() if roster is None else roster
    rrec = roster.get(aid, {})
    spawn_cmd = rec.get("spawn_cmd") or rrec.get("spawn_cmd", "")
    model = rec.get("model") or rrec.get("model", "")
    st = status_for(aid)
    summary = (st or {}).get("summary") or rec.get("summary", "")
    state = rec.get("state", "alive")
    status = display_status(st)
    if state == "unauthenticated":
        # The lifecycle hook fires on prompt-submit, so an agent that received a ping it can never
        # answer is frozen at "working" forever. Reporting that alongside a dead credential is the
        # false green this card exists to kill (8a6ebe4e48): it is blocked, on login.
        status = "blocked"
        summary = rec.get("auth_detail") or "not authenticated — run `claude auth login` on this node"
    row = {
        "agent_id": aid,
        "host": host,
        "session": rec.get("session", C.parse_agent_id(aid)[1]),
        "tab": rec.get("tab", C.parse_agent_id(aid)[2]),
        "backend": rec.get("backend", "claude"),
        "state": state,
        "boss_id": rec.get("boss_id", ""),
        "is_master": rec.get("is_master", False),
        "summary": summary,
        "status": status,
        "ts": rec.get("ts", now()),
        "tmux_target": tgt,
        "attach_base": ab,
        "attach_url": attach_url,
        "spawn_cmd": spawn_cmd,
        "revive_cmd": "mp revive %s" % aid,
        "model": model,
    }
    return row


def reap(t):
    # This host's own client going quiet means this Mac is slow or the client is restarting, not that
    # its agents died: reaping them emptied the Graph at load 94 with all 35 alive (card a4607576c1).
    # A retired agent leaves through /agents/unregister, whatever the heartbeat is doing.
    dead_hosts = [h for h, c in CLIENTS.items() if h != HOST_ID and t - c.get("last_seen", 0) > CLIENT_TTL]
    for h in dead_hosts:
        del CLIENTS[h]
    # prune agents whose host is gone/stale
    for aid in list(AGENTS.keys()):
        rec = AGENTS[aid]
        h = rec.get("host", "")
        if h == HOST_ID:
            continue
        c = CLIENTS.get(h)
        if not c or (t - c.get("last_seen", 0) > DEAD_AFTER):
            del AGENTS[aid]
    # expire delivered tasks older than 5m
    for tid in list(TASKS.keys()):
        if TASKS[tid].get("done_ts") and t - TASKS[tid]["done_ts"] > 300:
            del TASKS[tid]


def reaper():
    while True:
        time.sleep(5)
        with LOCK:
            reap(now())


def upsert_agents(host, agents):
    for a in agents or []:
        aid = a.get("agent_id")
        if not aid:
            continue
        h, sess, tab = C.parse_agent_id(aid)
        rec = AGENTS.get(aid, {})
        rec.update(
            {
                "agent_id": aid,
                "host": a.get("host") or host or h,
                "session": a.get("session") or sess,
                "tab": a.get("tab") or tab,
                "backend": a.get("backend", rec.get("backend", "claude")),
                "state": a.get("state", "alive"),
                "boss_id": a.get("boss_id", rec.get("boss_id", "")),
                "is_master": a.get("is_master", rec.get("is_master", False)),
                "auth_detail": a.get("auth_detail", ""),
                "summary": a.get("summary", rec.get("summary", "")),
                "spawn_cmd": a.get("spawn_cmd", rec.get("spawn_cmd", "")),
                "model": a.get("model", rec.get("model", "")),
                "tmux_target": a.get("tmux_target") or C.tmux_target(aid),
                "ts": now(),
            }
        )
        AGENTS[aid] = rec
