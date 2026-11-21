"""mp - MyPlow CLI. verbs: status spawn send peek kill answer revive model cron.
Implements the §4 agent_id<->tmux mapping and the §5 spawn contracts.

Each verb is a do_* function, in the order main() registers them: status,
spawn, send, peek, kill, answer, revive, model, ensure-boss, reconcile,
stop-recorders, board, card, comment, proof, owner, cron, tcc, pair, access,
plugins. Role resolution is delegated to mprole; this file does the tmux and
roster work.
"""

import os
import re
import sys
import json
import time
import shlex
import subprocess
import shutil
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import mprole
import mpplugins

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
QUEUE_URL = CFG["QUEUE_URL"]
SECRET = CFG.get("QUEUE_SECRET", "")
DEFAULT_BACKEND = CFG.get("DEFAULT_BACKEND", "claude")
# Backend for NEW agents spawned without --backend (what the Boss does when it hires). Split from
# DEFAULT_BACKEND on purpose: ensure-boss counts the Boss alive only if its window runs
# DEFAULT_BACKEND, so moving that knob to change new agents would make the supervisor "recover" a
# healthy Boss within one tick. Unset means the same as DEFAULT_BACKEND, as before.
DEFAULT_AGENT_BACKEND = (CFG.get("DEFAULT_AGENT_BACKEND", "") or DEFAULT_BACKEND).strip().lower()
DEFAULT_CLAUDE_MODEL = CFG.get("DEFAULT_CLAUDE_MODEL", CFG["DEFAULT_ENG_MODEL"])
DEFAULT_CODEX_MODEL = CFG.get("DEFAULT_CODEX_MODEL", "")
DEFAULT_GROK_MODEL = CFG.get("DEFAULT_GROK_MODEL", "")
VALID_BACKENDS = ("claude", "codex", "grok")
# A retirement is ACCIDENTAL only when the system itself observed the window/server vanish. Any
# other reason is a deliberate stop and is never auto-restarted -- not by reconcile, and (since
# card 0cc0bde980) not by ensure-boss either. roster.json is the single durable store of that
# intent: no second flag file exists that could disagree with it.
ACCIDENTAL_REASONS = frozenset(
    {"died-no-window", "tmux-server-crash", "window-missing", "boss-window-missing", "zombie-no-session"}
)
ROSTER_PATH = os.path.join(INSTALL_DIR, "run", "roster.json")
STATUS_DIR = os.path.join(INSTALL_DIR, "status")
BOSS_DOC = os.path.join(INSTALL_DIR, "mp-boss-doctrine.md")
ROLE_DIR = os.path.join(INSTALL_DIR, "roles")  # canonical read-only role store
ROLE_BUNDLE_DIR = os.path.join(INSTALL_DIR, "run", "role-bundles")  # derived per-agent views
HDR = {"X-Queue-Secret": SECRET}
TODO_URL = "http://127.0.0.1:%s" % CFG.get("TODO_PORT", "9933")  # board/todo-server origin
DOCTRINE_SUMMARY = (
    "Boss onboarded: plan-gate before engineering, drive the team autonomously "
    "off the board via the mp queue (fire-and-forget), verify every result."
)
# Spawn readiness: SessionStart hook writes session_id. Under memory pressure Claude can take
# much longer than a few seconds; default 45s balances false zombies vs blocking Boss.
SPAWN_READY_SEC = float(os.environ.get("MYPEOPLE_SPAWN_READY_SEC") or CFG.get("MYPEOPLE_SPAWN_READY_SEC") or 45)
# Birth-zombie: window+PID exist, status stuck at starting, session_id never written.
ZOMBIE_STARTING_SEC = float(
    os.environ.get("MYPEOPLE_ZOMBIE_STARTING_SEC") or CFG.get("MYPEOPLE_ZOMBIE_STARTING_SEC") or 90
)
ZOMBIE_REAP_MAX = int(os.environ.get("MYPEOPLE_ZOMBIE_REAP_MAX") or CFG.get("MYPEOPLE_ZOMBIE_REAP_MAX") or 3)


def resolve_role(role, backend, locked_role_ref=None):
    """Resolve + fail-closed-validate a role tag against the canonical store (reads mp.ROLE_DIR)."""
    return mprole.resolve_role(role, backend, ROLE_DIR, BOSS_DOC, locked_role_ref=locked_role_ref)


def materialize_role(item, aid, backend):
    """Materialize the derived per-agent bundle view (reads mp.ROLE_BUNDLE_DIR)."""
    return mprole.materialize_role(item, aid, backend, ROLE_BUNDLE_DIR)


def full_aid(aid):
    full = aid if "/" in aid else "%s/%s" % (HOST_ID, aid)
    # An agent name never starts with '-'. `mp spawn --help` once spawned a real agent named
    # --help and `mp kill --help` retired its roster entry (card d7679b9123).
    if any(part.startswith("-") for part in C.parse_agent_id(full)):
        sys.exit("ERROR: %r is not an agent name (no part may start with '-'); see mp <verb> --help" % aid)
    return full


def is_local(aid):
    host, _, _ = C.parse_agent_id(aid)
    return not host or host == HOST_ID


def remote_task(typ, aid, payload=None, timeout=150):
    """Submit a cross-host command through the queue and wait for its result."""
    code, out = C.http_json(
        "POST", QUEUE_URL + "/task/submit", {"type": typ, "target_agent": aid, "payload": payload or {}}, HDR, timeout=8
    )
    if code != 200 or not isinstance(out, dict) or not out.get("task_id"):
        return False, "queue submit failed (HTTP %s)" % code
    tid = out["task_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, state = C.http_json("GET", QUEUE_URL + "/task/" + tid, None, HDR, timeout=8)
        if code == 200 and isinstance(state, dict) and state.get("ok") is not None:
            return bool(state.get("ok")), state.get("result", "")
        time.sleep(0.5)
    return False, "queue task timed out: %s" % tid


def envq(**kw):
    return " ".join("%s=%s" % (k, shlex.quote(str(v))) for k, v in kw.items())


def load_roster():
    # Tracked read: pairs with save_roster's locked merge so a concurrent writer cannot
    # erase what we change (card e949527956).
    return C.read_json_tracked(ROSTER_PATH, {}) or {}


def save_roster(r):
    C.write_json_merged(ROSTER_PATH, r)


def pretrust(cwd):
    """Merge projects[cwd].hasTrustDialogAccepted=true into ~/.claude.json (§5.5c)."""
    p = os.path.expanduser("~/.claude.json")
    d = C.read_json(p, {}) or {}
    if not isinstance(d, dict):
        d = {}
    proj = d.setdefault("projects", {})
    for path in {cwd, os.path.abspath(cwd)}:
        rec = proj.get(path, {})
        rec["hasTrustDialogAccepted"] = True
        proj[path] = rec
    C.write_json(p, d)


def claude_consent_settings():
    """Path to a settings file that declares the bypass-permissions consent, written on demand.

    A role-mounted spawn carries the consent in its own bundle overlay, but a BUNDLE-LESS claude
    spawn gets no --settings at all -- so it would fall back to whatever ~/.claude happens to hold
    and, on a node that was never seeded, block on the Bypass Permissions modal whose default
    button is "No, exit" (card 293fc81898). Every claude launch must be self-sufficient, so hand
    the bundle-less ones this file instead. Returns None if it cannot be written; the launch then
    degrades to the ~/.claude path rather than failing outright.
    """
    path = os.path.join(INSTALL_DIR, "run", "claude-consent.settings.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        C.write_json(path, {"skipDangerousModePermissionPrompt": True, "deniedMcpServers": mprole.CLAUDE_DENIED_MCP})
        return path
    except OSError:
        return None


def codex_trust_override(cwd):
    """Trust only an exact cwd inside MyPlow's managed agent run roots for this launch."""
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    managed_roots = (
        os.path.realpath(os.path.join(INSTALL_DIR, "run", "eng")),
        os.path.realpath(os.path.join(INSTALL_DIR, "run", "boss")),
    )
    for root in managed_roots:
        try:
            if os.path.commonpath((resolved, root)) == root:
                # A single `projects` inline table is accepted by Codex's --config parser. It
                # applies only to this process, so unrelated user trust entries are untouched.
                return 'projects={%s={trust_level="trusted"}}' % json.dumps(resolved)
        except ValueError:
            continue
    return ""


def grok_pretrust():
    """Disable Grok's project-directory picker (§5.5c analogue for grok).

    With a non-git cwd (run/eng is not a repo) and prior sessions on record, Grok's TUI
    answers the first message with a "Run Grok Build in a project directory?" modal that
    swallows it. This is the key Grok's own "Don't ask me again" writes.

    Only the bundle-less path needs this: a --role grok agent gets the hint baked into its
    per-agent GROK_HOME config by mprole._grok_config_toml.
    """
    import re

    root = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
    p = os.path.join(root, "config.toml")
    s = ""
    if os.path.exists(p):
        try:
            with open(p) as f:
                s = f.read()
        except Exception:
            return
    m = re.search(r"^hints\s*=\s*\{([^}]*)\}[ \t]*$", s, re.M)
    if m:
        inner = m.group(1)
        if re.search(r"project_picker_disabled\s*=\s*true", inner):
            return
        if "project_picker_disabled" in inner:
            inner = re.sub(r"project_picker_disabled\s*=\s*\w+", "project_picker_disabled = true", inner)
        else:
            head = inner.strip().rstrip(",")
            inner = (head + ", " if head else " ") + "project_picker_disabled = true "
        out = s[: m.start()] + "hints = {%s}" % inner + s[m.end() :]
    elif re.search(r"^[ \t]*\[hints\][ \t]*$", s, re.M):
        if re.search(r"^[ \t]*project_picker_disabled\s*=", s, re.M):
            return
        out = re.sub(r"^([ \t]*\[hints\][ \t]*\n)", r"\1project_picker_disabled = true\n", s, count=1, flags=re.M)
    else:
        # a bare key must precede the first [section] header
        out = "hints = { project_picker_disabled = true }\n" + s
    try:
        import tomllib

        parsed = tomllib.loads(out)
        if not parsed.get("hints", {}).get("project_picker_disabled"):
            return
    except Exception:
        return  # never leave the user's grok config unparseable
    try:
        os.makedirs(root, exist_ok=True)
        C.atomic_write(p, out.encode("utf-8"))
    except Exception:
        pass


def write_status(aid, status, backend, summary=None, locked=False, drop_session=False):
    _, sess, tab = C.parse_agent_id(aid)
    d = os.path.join(STATUS_DIR, "mc-%s" % sess)
    p = os.path.join(d, "%s.json" % tab)
    cur = C.read_json(p, {}) or {}
    if drop_session:
        cur.pop("session_id", None)
    cur.update({"status": status, "timestamp": time.time(), "backend": backend})
    if summary is not None:
        cur["summary"] = summary
    if locked:
        cur["summary_locked"] = True
    C.write_json(p, cur)


def status_path(aid):
    _, sess, tab = C.parse_agent_id(aid)
    return os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab)


def read_agent_status(aid):
    return C.read_json(status_path(aid), {}) or {}


def pane_looks_ready(aid):
    """True when the backend TUI has painted a usable composer (session_id may still be empty).

    Claude 2.1.x often paints the welcome screen before any lifecycle hook writes session_id;
    session_id is frequently first persisted on UserPromptSubmit (first successful mp send).
    """
    try:
        pane = C.tmux_capture(C.tmux_target(aid), lines=80)
    except Exception:
        return False
    if not (pane or "").strip():
        return False
    low = pane.lower()
    markers = (
        "welcome back",
        "bypass permissions",
        "claude code",
        "esc to interrupt",
        "ctrl+c:cancel",
        "codex",
        "grok build",
        "shift+tab",
    )
    # bare composer prompt alone is weak (shell too); require a product marker
    return any(m in low for m in markers)


def pane_looks_working(aid):
    """True when the pane shows an in-flight turn or a sign-in screen (do not reap as zombie).

    A sign-in screen never reports a session, but a respawn kills the device-code login the
    user may be finishing in it (delattre1/mypeople#35).
    """
    try:
        pane = C.tmux_capture(C.tmux_target(aid), lines=80)
    except Exception:
        return False
    low = (pane or "").lower()
    return (
        "waiting for response" in low
        or "esc to interrupt" in low
        or "ctrl+c:cancel" in low
        or "tokens" in low
        or "sign in with chatgpt" in low
        or "sign in with device code" in low
        or "one-time code" in low
        or "select login method" in low
    )


def wait_for_backend_ready(aid, timeout=None, interval=0.5):
    """Wait until session_id is written OR the TUI is visibly up.

    Returns (session_id_or_empty, reason) where reason is 'session' | 'pane' | ''.
    """
    timeout = SPAWN_READY_SEC if timeout is None else timeout
    deadline = time.time() + max(0.0, float(timeout))
    p = status_path(aid)
    while time.time() < deadline:
        st = C.read_json(p, {}) or {}
        sid = (st.get("session_id") or "").strip()
        if sid:
            return sid, "session"
        if pane_looks_ready(aid):
            return "", "pane"
        time.sleep(interval)
    st = C.read_json(p, {}) or {}
    sid = (st.get("session_id") or "").strip()
    if sid:
        return sid, "session"
    if pane_looks_ready(aid):
        return "", "pane"
    return "", ""


def is_birth_zombie(aid, rr, now=None):
    """True when a live window never became a working agent (eng-170 class).

    Signal triad (card 157dcb7c75):
      1) roster/status session_id empty  (no UserPromptSubmit ever succeeded)
      2) status stuck at 'starting' (or missing)
      3) status timestamp older than ZOMBIE_STARTING_SEC
    Plus pane is blank OR idle-welcome (not actively working). Window must exist.
    """
    now = time.time() if now is None else now
    if not rr or rr.get("retired") or rr.get("is_master"):
        return False
    _, sess, tab = C.parse_agent_id(aid)
    if not sess or not tab:
        return False
    st = read_agent_status(aid)
    sid = (st.get("session_id") or rr.get("session_id") or "").strip()
    if sid:
        return False
    status = (st.get("status") or "starting").strip().lower()
    if status not in ("starting", ""):
        return False
    ts = float(st.get("timestamp") or 0) or 0.0
    if ts <= 0:
        return False
    if (now - ts) < ZOMBIE_STARTING_SEC:
        return False
    if not window_exists(sess, tab):
        return False
    if pane_looks_working(aid):
        return False
    # codex fires SessionStart only with its first prompt, so an idle codex TUI has no session
    # yet and is healthy; reaping it restarted signed-in engineers every 90s (#35)
    if (rr.get("backend") or st.get("backend")) == "codex" and pane_looks_ready(aid):
        return False
    # blank pane, idle welcome, or any non-working stuck starting without session_id
    return True


def window_exists(sess, tab):
    r = C.tmux_run("list-windows", "-t", "mc-%s" % sess, "-F", "#{window_name}", text=True)
    return r.returncode == 0 and tab in r.stdout.split()


def start_tmux_session(args, e):
    """`tmux new-session` that cannot strand the fleet.

    Refused by a stalled server, a plain new-session deletes the socket and starts a second server,
    rc 0, leaving every agent running where nothing can reach them. -N never starts one, so a new
    server is only started once tmux_server_gone says nothing is there.
    """
    r = C.tmux_run("-N", "new-session", *args, env=e, text=True)
    if r.returncode != 0 and C.tmux_server_gone(e):
        r = subprocess.run(["tmux", "new-session"] + list(args), env=e, capture_output=True, text=True)
    return r


def close_agent_window(sess, tab, e):
    """Close an agent's window and the web viewers parked on it. True once the window is gone.

    Names are exact (`=`): tmux falls back to a prefix match, so closing an already-gone eng-11
    closed eng-1155, a live agent, and said it worked. A viewer left on a dying window lands on a
    neighbour (Boss, measured) and types into it under the dead agent's name, so it goes too; one
    that moved to another window through the chooser is still in use and stays.
    """
    group = "mc-%s" % sess
    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_group}\t#{window_name}"],
        env=e,
        capture_output=True,
        text=True,
    ).stdout
    for name, grp, window in (l.split("\t") for l in listed.splitlines() if l.count("\t") == 2):
        if name.startswith("_v_%s_" % tab) and grp == group and window == tab:
            subprocess.run(["tmux", "kill-session", "-t", "=" + name], env=e, capture_output=True)
    subprocess.run(["tmux", "kill-window", "-t", "=%s:=%s" % (group, tab)], env=e, capture_output=True)
    return not window_exists(sess, tab)


# The permission-bypass flag each backend MUST be launched with. An agent without it stops on the
# first tool prompt and looks hung to whoever installed MyPlow -- the exact symptom reported for
# a first-run Boss (card 5211716904, items 6+7).
BYPASS_MARKERS = {
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "grok": ["--permission-mode", "bypassPermissions"],
}


def _pane_process_tree(sess, tab):
    """Command lines of every process under an agent window's panes, or None if undeterminable.

    None is NOT "nothing running": it means tmux or ps could not answer, and callers must not
    destroy a pane on that basis.
    """
    r = subprocess.run(
        ["tmux", "list-panes", "-t", "mc-%s:%s" % (sess, tab), "-F", "#{pane_pid}"], capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    roots = [p for p in r.stdout.split() if p.isdigit()]
    if not roots:
        return None
    ps = subprocess.run(["ps", "-eo", "pid=,ppid=,command="], capture_output=True, text=True)
    if ps.returncode != 0:
        return None
    children, cmd = {}, {}
    for line in ps.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, command = parts
        if not (pid.isdigit() and ppid.isdigit()):
            continue
        cmd[pid] = command
        children.setdefault(ppid, []).append(pid)
    seen, out, stack = set(), [], list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in cmd:
            out.append(cmd[pid])
        stack.extend(children.get(pid, []))
    return out


def window_runs_launched_agent(sess, tab, backend):
    """True/False/None: is this window running a correctly launched `backend` agent?

    A window existing is NOT proof an agent is in it. An interactive `claude auth login`, a
    leftover shell, or a dead pane all leave the window standing. do_spawn used to treat the mere
    window as success -- printing "reusing", writing the roster and returning 0 without ever
    running `launch` -- which is how a Boss came up without its bypass flag and why the Spawn
    buttons looked dead while Revive worked (Revive is only reachable once the window is gone, so
    it takes the branch that actually launches).

    None means "could not tell"; callers must treat it as leave-alone, never as a reason to kill.
    """
    tree = _pane_process_tree(sess, tab)
    if tree is None:
        return None
    markers = BYPASS_MARKERS.get(backend)
    if not markers:
        return None
    for command in tree:
        if backend in command and all(m in command for m in markers):
            return True
    return False


def session_exists(sess):
    return C.tmux_run("has-session", "-t", "mc-%s" % sess).returncode == 0


def tmux_env():
    e = dict(os.environ)
    e.pop("TMUX", None)
    return e


def recording_enabled_global():
    """Host-wide opt-in. Default OFF. Truthy: 1/true/yes/on."""
    v = (os.environ.get("MYPEOPLE_RECORD") or CFG.get("MYPEOPLE_RECORD") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def want_recorder(record_flag, prior):
    """Record only if --record, MYPEOPLE_RECORD=1, or roster already opted this agent in."""
    if record_flag:
        return True
    if recording_enabled_global():
        return True
    if isinstance(prior, dict) and prior.get("record"):
        return True
    return False


def start_recorder(tab, sess, previous=None):
    """Start a read-only asciinema recorder on the pane (opt-in only; §spawn-recording).
    Best-effort. Callers must gate on want_recorder(): the recorder attaches a READ-ONLY tmux
    client, and when it is the only client attached, tmux refuses send-keys with "client is
    read-only" -- i.e. an unconditional recorder makes a headless agent unable to be spoken to."""
    rec_sess = "rec-%s" % tab
    e = tmux_env()
    current = C.current_recorder(sess, tab, HOST_ID)
    previous_pid = previous.get("pid") if isinstance(previous, dict) else None
    if previous_pid and (not current or previous_pid != current.get("pid")):
        # A recorder from an older tmux socket generation is not reachable by session name.
        C.stop_recorder(sess, tab, previous, HOST_ID, include_current=False)
    if current:
        return current
    if subprocess.run(["tmux", "has-session", "-t", rec_sess], env=e, capture_output=True).returncode == 0:
        return None
    cast = C.recorder_identity(sess, tab, HOST_ID)["cast"]
    os.makedirs(os.path.dirname(cast), exist_ok=True)
    if not shutil.which("asciinema"):
        return
    inner = "TMUX= tmux attach -rt mc-%s:%s" % (sess, tab)
    cmd = "asciinema rec --quiet --append -c %s %s" % (shlex.quote(inner), shlex.quote(cast))
    try:
        r = subprocess.run(
            ["tmux", "-N", "new-session", "-d", "-s", rec_sess, cmd], env=e, capture_output=True, timeout=10
        )
        if r.returncode != 0:
            return None
        for _ in range(20):
            current = C.current_recorder(sess, tab, HOST_ID)
            if current:
                return current
            time.sleep(0.05)
    except Exception:
        return None
    return None


# A --community engineer works for someone outside the team (plugins/discord-chat). dontAsk refuses
# every tool not allowed here, reads and writes outside its folder included, and no MCP server is
# loaded (no Latch). Bash runs only inside Claude's macOS sandbox: it reads nothing in the home dir but
# its folder, reaches no host but GitHub and the package registries, and opens no Unix socket, so no
# tmux pane either. Proven on Mac 2 (card 53991ce89f): gh token, Keychain, queue.env, ~/.claude.json,
# :9900, ssh and example.com all refused; GitHub clone, commit, format-patch and pip work.
COMMUNITY_SETTINGS = {
    "permissions": {
        "allow": ["Edit(./**)"],
        "deny": [
            "Read(~/.config/**)",
            "Read(~/.ssh/**)",
            "Read(~/.claude.json)",
            "Read(~/Library/**)",
            "WebFetch",
            "WebSearch",
        ],
    },
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "filesystem": {"denyRead": ["~"], "allowRead": ["."]},
        "network": {
            "allowedDomains": [
                "github.com",
                "*.github.com",
                "*.githubusercontent.com",
                "pypi.org",
                "files.pythonhosted.org",
                "registry.npmjs.org",
            ],
            "strictAllowlist": True,
        },
    },
}
# The sandbox refuses ~/.gitconfig and any .git/config, so git skips the first and clones with a
# separate git dir (COMMUNITY_DOC); its commits carry a name that is not the owner's.
COMMUNITY_GIT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TEMPLATE_DIR": "/nonexistent",
    "GIT_AUTHOR_NAME": "MyPlow community engineer",
    "GIT_AUTHOR_EMAIL": "noreply@plow.co",
    "GIT_COMMITTER_NAME": "MyPlow community engineer",
    "GIT_COMMITTER_EMAIL": "noreply@plow.co",
}
COMMUNITY_DOC = """# You are a community engineer

You work for a developer outside the team, through the Boss. Your shell runs in a sandbox: it reads
and writes only this folder and reaches only GitHub and the package registries (pypi, npm). You
have no board, no mp, no MCP tools and no GitHub login. Report by finishing your turn: the Boss
reads your last message.

- Clone with a git dir not named .git (the sandbox refuses .git/config):
  git clone --separate-git-dir=REPO.git https://github.com/OWNER/REPO REPO
- Hand work back as patch files, never a push: git format-patch origin/HEAD -o ../patches
  and name the files in your last message. The Boss reviews them and pushes.
- Public repos only. Never put a key, token or secret in code, a patch or a message.
"""


def community_flags(role_flags):
    """The role's own settings (its lifecycle hooks) plus the lock, as one --settings JSON."""
    settings = {}
    if "--settings" in role_flags:
        i = role_flags.index("--settings")
        with open(role_flags[i + 1]) as f:
            settings = json.load(f)
        role_flags = role_flags[:i] + role_flags[i + 2 :]
    settings.pop("skipDangerousModePermissionPrompt", None)
    settings.update(COMMUNITY_SETTINGS)
    return role_flags + ["--settings", json.dumps(settings)]


def build_launch(
    aid, cwd, boss_id, is_master, model, backend, resume_session=None, role_bundle=None, community=False, effort=""
):
    exports = {
        "PATH": "%s/.local/bin:%s/bin:%s" % (os.path.expanduser("~"), INSTALL_DIR, os.environ.get("PATH", "/usr/bin")),
        "AGENT_ID": aid,
        "QUEUE_URL": QUEUE_URL,
        # Never QUEUE_SECRET: this string is the launch command, which ps shows every user. The agent
        # gets the secret from the tmux server's env (supervise.sh sources queue.env), and mp and the
        # hooks read queue.env through this path.
        "MYPEOPLE_CONFIG_PATH": C.CONFIG_PATH,
        "HOST_ID": HOST_ID,
        "INSTALL_DIR": INSTALL_DIR,
        "MYPEOPLE_BACKEND": backend,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # non-secret role identity env (MYPEOPLE_ROLE*, plus CODEX_HOME / GROK_HOME for the adapters
    # that mount through a home dir instead of flags). Absent role_bundle, the launch is byte-for-
    # byte the legacy one -- a bundle-less agent keeps working exactly as before.
    role_flags = []
    if role_bundle:
        exports.update(mprole.role_env(role_bundle))
        # dispatch explicitly: an else-branch here silently hands one adapter's flags to another
        # backend, which rejects them -- the agent would die at launch with the role "mounted".
        role_flags = {"claude": mprole.claude_flags, "codex": mprole.codex_flags, "grok": mprole.grok_flags}[backend](
            role_bundle
        )
    # The window inherits the tmux server's env, where MyPlow.app's PYTHONPATH (its bundled, older
    # `mypeople`) shadows the installed CLI for everything the agent runs (card 70a20b5dcf).
    prefix = "env -u PYTHONPATH"
    if is_master:
        prefix += " -u BOSS_ID"  # top-level agent must not inherit a leaked boss
    else:
        exports["BOSS_ID"] = boss_id
    if community:
        # None of the tmux server's env (queue.env's secrets, the ssh agent): only what it runs on.
        # Its hooks read the queue secret from queue.env themselves, outside its sandbox.
        prefix = "env -i"
        exports.update(
            {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR") if os.environ.get(k)}
        )
        exports.update(COMMUNITY_GIT)
        role_flags = community_flags(role_flags)
    envstr = envq(**exports)
    if backend == "claude":
        agent = (
            "claude --permission-mode dontAsk --strict-mcp-config --mcp-config '{\"mcpServers\": {}}'"
            if community
            else "claude --dangerously-skip-permissions"
        )
        if role_flags:
            agent += " " + " ".join(shlex.quote(f) for f in role_flags)
        if "--settings" not in role_flags:
            # bundle-less spawn: nothing else would carry the consent (card 293fc81898)
            consent = claude_consent_settings()
            if consent:
                agent += " --settings %s" % shlex.quote(consent)
        if model:
            agent += " --model %s" % shlex.quote(model)
        if effort:
            agent += " --effort %s" % shlex.quote(effort)
        if resume_session:
            agent += " --resume %s" % shlex.quote(resume_session)
    elif backend == "codex":
        agent = "codex --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust"
        trust_override = codex_trust_override(cwd)
        if trust_override:
            agent += " --config %s" % shlex.quote(trust_override)
        if role_flags:
            agent += " " + " ".join(shlex.quote(f) for f in role_flags)
        if model:
            agent += " --model %s" % shlex.quote(model)
        if resume_session:
            agent += " resume %s" % shlex.quote(resume_session)
    elif backend == "grok":
        agent = "grok --permission-mode bypassPermissions"
        if role_flags:
            agent += " " + " ".join(shlex.quote(f) for f in role_flags)
        if model:
            agent += " --model %s" % shlex.quote(model)
        if resume_session:
            agent += " --resume %s" % shlex.quote(resume_session)
    else:
        raise ValueError("unsupported backend: %s" % backend)
    return "%s %s %s" % (prefix, envstr, agent)
