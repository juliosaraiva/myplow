"""MyPlow todo-server (:9933): board API + board->Boss ping.
Serves todos.html at / and /todos, and reverse-proxies the HUD routes so the
cross-nav works from either front door. Python 3 stdlib only.
MODULE-LEVEL imports of every stdlib a handler uses (§5.3b UnboundLocalError guard).

Sections, in order: watchdog jobs, the board store (JSON or SQLite), the
board-to-Boss ping, card ownership, proof classification, then Handler
(GET/POST for the board, plugins, and access pages). Pages and fonts are read
from this file's directory.
"""

import os
import sys
import json
import time
import uuid
import threading
import shutil
import subprocess
import glob
import re
import copy
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import mpplugins
import boardstore as BS

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
HUD_PORT = int(CFG["HUD_PORT"])
TODO_PORT = int(CFG["TODO_PORT"])
SECRET = CFG.get("QUEUE_SECRET", "")
BOSS_AGENT = "%s/main:Boss" % HOST_ID
TODOS_DIR = os.path.join(INSTALL_DIR, "todos")
BOARD_PATH = os.path.join(TODOS_DIR, "board.v2.json")
PROOFS_DIR = os.path.join(TODOS_DIR, "proofs")
INBOX_LOG = os.path.join(TODOS_DIR, "boss-inbox.log")
HTML_DIR = os.path.dirname(os.path.abspath(__file__))
TODOS_HTML = os.path.join(HTML_DIR, "todos.html")
TERMINAL_GRAPH_HTML = os.path.join(HTML_DIR, "terminal-graph.html")
PLUGINS_HTML = os.path.join(HTML_DIR, "plugins.html")
ACCESS_HTML = os.path.join(HTML_DIR, "access.html")
A2A = os.path.join(INSTALL_DIR, "plugins", "a2a", "a2a.py")
FONT_TYPES = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8"}

VALID_STATES = {"working", "review", "done", "blocked", "cancelled", "recurring"}
# A card in a terminal state has no work left, so it must have no living owner.
TERMINAL_STATES = {"done", "cancelled"}
FULL_AGENT_ID = re.compile(r"^[^/\s]+/[^/:\s]+:[^/:\s]+$")
# Engines offerable for a Boss spawn (card 0cc0bde980). Mirrors mp's VALID_BACKENDS; `mp` remains
# the authority and rejects anything it does not support, so this is only a UX guard.
BOSS_BACKENDS = ("claude", "codex", "grok")
LOCK = threading.RLock()
START = time.time()
