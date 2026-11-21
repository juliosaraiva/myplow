"""MyPlow CLI (command: mypeople) — thin wrapper over the existing daemons/supervisor.

  mypeople up [--server|--client|--both] [--backend claude|codex|grok] [--detach]
                                                        bring the stack up (default: both, foreground)
  mypeople            (no verb)                        alias for `up` (what `uvx mypeople` runs)
  mypeople down                                        stop daemons (board/roster kept on disk)
  mypeople status                                      health + agents + roster
  mypeople auth-check [--quiet]                        re-check this node's AI login (exit 0 = ready)
  mypeople verify                                      run the shipped acceptance harness
  mypeople logs [name]                                 tail INSTALL_DIR/logs/*.log

The runtime this command copies and supervises is mypeople/runtime/. The map
is docs/ARCHITECTURE.md.
"""

import os
import sys
import time
import json
import signal
import subprocess
import urllib.request
from . import firstrun, __version__

CONFIG_PATH = firstrun.CONFIG_PATH
load_cfg = firstrun.load_cfg  # desktop/src-tauri/src/main.rs calls cli.load_cfg()


# A daemon is nobody's agent. `up` runs from whatever launched it, and an agent's shell would lend
# every daemon its AGENT_ID/BOSS_ID, tmux client and Claude session: each `mp send` a daemon made
# then claimed to come from that agent (card 8f490e73e5: the iMessage bridge sent the Boss's
# replies to eng-961). Stripped on the way out, whoever the caller is.
# Named, never by a vendor prefix: a cloud MyPlow's Claude login (CLAUDE_CODE_OAUTH_TOKEN) and its
# settings (CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC) live in CLAUDE_CODE_* variables too, and
# stripping the prefix logged every cloud Boss out.
AGENT_IDENTITY = (
    "AGENT_ID",
    "BOSS_ID",
    "MYPEOPLE_BACKEND",
    "TMUX",
    "TMUX_PANE",
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
)
AGENT_IDENTITY_PREFIXES = ("MYPEOPLE_ROLE",)


def child_env(cfg):
    e = {k: v for k, v in os.environ.items() if k not in AGENT_IDENTITY and not k.startswith(AGENT_IDENTITY_PREFIXES)}
    e.update(cfg)  # queue.env values available to daemons
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    e["MYPEOPLE_CONFIG_PATH"] = CONFIG_PATH
    e["MYPEOPLE_HOME"] = install
    e["PATH"] = "%s/.local/bin:%s/bin:%s" % (os.path.expanduser("~"), install, e.get("PATH", "/usr/bin:/bin"))
    # Authentication stays in the selected CLI's node-local credential store.
    return e


def _get(url, secret=None, timeout=6):
    req = urllib.request.Request(url)
    if secret:
        req.add_header("X-Queue-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except Exception:
        return 0, None


def wait_health(cfg, seconds=40):
    url = "http://127.0.0.1:%s/health" % cfg.get("HUD_PORT", "9900")
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, body = _get(url)
        if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return True
        time.sleep(1)
    return False


def _serving_version_path(install):
    return os.path.join(install, "run", "serving.version")


_daemons_running = firstrun.daemons_running


def restart_if_serving_stale(cfg, install):
    """An upgrade rewrites the daemon code on disk, but supervise.sh is CHECK-BEFORE-SPAWN: it
    never respawns a daemon that is already alive. So an upgraded node keeps SERVING the old code
    from memory -- old pages, old version badge -- until something cycles it. `mypeople up` is that
    something. Returns True if it restarted.

    A node whose daemons predate this stamp has no file: treat that as stale exactly once, which
    costs a few seconds and gets everyone onto the version they actually installed.
    """
    if not _daemons_running(install):
        return False
    if firstrun.refuse_downgrade(install):
        # An older app must not cycle a newer install's daemons either: they are running the
        # install's own, newer code, which is exactly what should keep serving.
        return False
    stamp = _serving_version_path(install)
    try:
        with open(stamp) as f:
            serving = f.read().strip()
    except Exception:
        serving = ""
    if serving == __version__:
        return False
    print(
        "[myplow] daemons are serving %s, installed is %s — restarting them"
        % (serving or "an older build", __version__)
    )
    cmd_down([])
    time.sleep(1)
    return True


def _stamp_serving_version(install):
    # What supervise runs is the install's code, which is THIS package only if materialize copied
    # it. An older app that left a newer install alone stamped its own version here (card 8f490e73e5).
    try:
        os.makedirs(os.path.join(install, "run"), exist_ok=True)
        with open(_serving_version_path(install), "w") as f:
            f.write((firstrun.installed_version(install) or __version__) + "\n")
    except OSError:
        pass


def urls(cfg):
    return {
        "board": "http://localhost:%s" % cfg.get("TODO_PORT", "9933"),
        "HUD": "http://localhost:%s/dashboard" % cfg.get("HUD_PORT", "9900"),
        "term": "http://localhost:%s" % cfg.get("TTYD_BROWSER_PORT", cfg.get("TTYD_PORT", "7681")),
    }


def print_login_required(cfg):
    print("\n  " + "=" * 66)
    print("  MyPlow is UP but this node has no AI login yet.")
    print("  The board and the HUD below work; no agent can run until you log in.\n")
    for line in firstrun.login_howto():
        print("    %s" % line)
    print("\n  The Boss starts by itself within ~15s of a successful login —")
    print("  you do NOT need to restart MyPlow.")
    print("  " + "=" * 66)


def print_urls(cfg):
    u = urls(cfg)
    print("\n  MyPlow is up:")
    print("    board  → %s" % u["board"])
    print("    HUD    → %s" % u["HUD"])
    print("    term   → %s" % u["term"])
    print()
