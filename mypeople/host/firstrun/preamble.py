"""First-run entrypoint — REPLACES seed hydration with configuration only (never code-gen).

ensure() is idempotent: safe on every `up` / container restart. It materializes a writable
INSTALL_DIR from the packaged runtime, resolves the selected backend's auth, writes the single
config file (~/.config/mypeople/queue.env, fresh QUEUE_SECRET per install), wires Claude/Codex
lifecycle hooks, and installs the functional tmux.conf. Starting daemons + spawning the Boss is
the CLI's job (see cli.up).

The steps below are marked in order: materialize, auth, queue.env, backend
hooks, tmux.conf, then ensure() which runs them.
"""

import os
import sys
import json
import shutil
import secrets
import socket
import stat
import subprocess
import shlex
import time
import hashlib

VALID_BACKENDS = ("claude", "codex", "grok")
LIFECYCLE_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")


def _config_path():
    explicit = os.environ.get("MYPEOPLE_CONFIG_PATH")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.environ.get("MYPEOPLE_HOME")
    if home:
        return os.path.join(os.path.abspath(os.path.expanduser(home)), "config", "queue.env")
    return os.path.expanduser("~/.config/mypeople/queue.env")


CONFIG_PATH = _config_path()
CONFIG_DIR = os.path.dirname(CONFIG_PATH)


def load_cfg():
    """queue.env as {key: value}: `export ` and one pair of matching quotes stripped."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        for line in open(CONFIG_PATH):
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


def runtime_dir():
    """Absolute path to the packaged runtime tree (bin/, plugins/, verify/, ...)."""
    try:
        from importlib.resources import files

        return str(files("mypeople") / "runtime")
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")


def install_dir():
    return os.path.abspath(os.path.expanduser(os.environ.get("MYPEOPLE_HOME") or "~/.local/share/mypeople"))


def _echo(msg):
    sys.stderr.write(msg + "\n")
