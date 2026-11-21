"""MyPlow shared helpers: config, auth/session, json io, tmux delivery, http proxy.
Python 3 stdlib only.

Imported as a sibling (`import mpcommon`) by the daemons and by `mp`. The banners
in this file are the layout; the pieces stay here because the install copies
`bin/` flat.

  load_env, version, live_drift     config and the serving version
  render_page                       badge, nav, login banner
  mint_session, caller_identity     the page-load cookie
  atomic_write, read_json           json on disk
  parse_agent_id and below          agent ids, notification routes, plugin text
  current_recorder, stop_recorder   the read-only terminal recorder
  tmux_run, tmux_send_message       delivery into a pane
  http_json, WebSocket              clients used by the bridges
  JSONHandler                       body limits, sessions, proxying, the thread cap
"""

import os
import sys
import json
import hmac
import hashlib
import base64
import html
import time
import socket
import ssl
import subprocess
import shutil
import urllib.request
import urllib.parse
import errno
import re
import mimetypes
import shlex
import signal
import copy
import fcntl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def config_path():
    explicit = os.environ.get("MYPEOPLE_CONFIG_PATH")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.environ.get("MYPEOPLE_HOME")
    if home:
        return os.path.join(os.path.abspath(os.path.expanduser(home)), "config", "queue.env")
    return os.path.expanduser("~/.config/mypeople/queue.env")


CONFIG_PATH = config_path()


def read_config_file():
    """queue.env as it is on disk now, without the env override load_env() applies.

    A long-lived process (the Boss) snapshotted its env at launch, so for a knob meant to move
    without a restart -- the default models -- the file is the current answer."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                    v = v[1:-1]
                cfg[k.strip()] = v
    return cfg


def load_env():
    cfg = read_config_file()
    # Live env overrides the file, including fleet/client-only keys not present in old configs.
    known = set(cfg) | {
        "INSTALL_DIR",
        "HOST_ID",
        "HUD_PORT",
        "TODO_PORT",
        "TTYD_PORT",
        "TTYD_BROWSER_PORT",
        "TTYD_RO_PORT",
        "BIND_ADDR",
        "QUEUE_URL",
        "QUEUE_SECRET",
        "TTYD_PUBLIC_URL",
        "DEFAULT_ENG_MODEL",
        "DEFAULT_BACKEND",
        "DEFAULT_AGENT_BACKEND",
        "DEFAULT_CLAUDE_MODEL",
        "DEFAULT_CLAUDE_EFFORT",
        "DEFAULT_CODEX_MODEL",
        "DEFAULT_GROK_MODEL",
        "MYPEOPLE_RECORD",
        "QUEUE_DEAD_AFTER",
        "HEARTBEAT_INTERVAL",
        "UPSTREAM_QUEUE_URL",
        "UPSTREAM_QUEUE_SECRET",
        "NODE_PURPOSE",
        "NODE_TYPE",
        "NODE_RECORDING_URL",
        "MP_HANDLER_TIMEOUT",
        "MP_MAX_CONNECTIONS",
        "MP_MAX_JSON_BYTES",
        "MP_MAX_UPLOAD_BYTES",
        "MP_SESSION_TTL",
    }
    for k in known:
        if os.environ.get(k) is not None:
            cfg[k] = os.environ[k]
    # a few defaults
    cfg.setdefault("INSTALL_DIR", os.path.expanduser("~/mypeople"))
    cfg.setdefault("HOST_ID", socket.gethostname().split(".")[0])
    cfg.setdefault("HUD_PORT", "9900")
    cfg.setdefault("TODO_PORT", "9933")
    cfg.setdefault("TTYD_PORT", "7681")
    cfg.setdefault("TTYD_BROWSER_PORT", cfg["TTYD_PORT"])
    # Read-only ttyd: the Terminal Graph's tiles are views, not consoles. Stock ttyd is readonly
    # unless -W, so this is a second plain ttyd rather than a special build.
    cfg.setdefault("TTYD_RO_PORT", str(int(cfg["TTYD_PORT"]) + 1))
    # Loopback unless the operator publishes the port. A reachable listener hands the
    # owner UI a session cookie (see mint_session); binding every interface makes that
    # cookie available to the network. Docker sets BIND_ADDR=0.0.0.0 itself.
    cfg.setdefault("BIND_ADDR", "127.0.0.1")
    cfg.setdefault("QUEUE_URL", "http://127.0.0.1:%s" % cfg["HUD_PORT"])
    cfg.setdefault("DEFAULT_ENG_MODEL", "claude-opus-5-5")
    cfg.setdefault("DEFAULT_BACKEND", "claude")
    cfg.setdefault("DEFAULT_CLAUDE_MODEL", cfg["DEFAULT_ENG_MODEL"])
    cfg.setdefault("DEFAULT_CLAUDE_EFFORT", "xhigh")
    cfg.setdefault("DEFAULT_CODEX_MODEL", "")
    cfg.setdefault("QUEUE_DEAD_AFTER", "45")
    cfg.setdefault("HEARTBEAT_INTERVAL", "10")
    # Idle socket timeout (seconds). 0 disables. A partial or abandoned connection
    # otherwise pins a thread for the life of the process.
    cfg.setdefault("MP_HANDLER_TIMEOUT", "30")
    # Simultaneous accepted connections. 0 disables the cap.
    cfg.setdefault("MP_MAX_CONNECTIONS", "128")
    # JSON API bodies, and multipart proof uploads. Oversized requests are 413.
    cfg.setdefault("MP_MAX_JSON_BYTES", str(1024 * 1024))
    cfg.setdefault("MP_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024))
    # Page-load session lifetime. The token itself carries the expiry.
    cfg.setdefault("MP_SESSION_TTL", str(12 * 3600))
    return cfg


CFG = load_env()
