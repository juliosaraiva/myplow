# ---------------------------------------------------------------- step 2: auth
def _authcheck():
    """Load the runtime's authcheck module (shared with the daemons, which cannot import us)."""
    import importlib.machinery, importlib.util
    path = os.path.join(runtime_dir(), "bin", "authcheck.py")
    loader = importlib.machinery.SourceFileLoader("mypeople_authcheck", path)
    spec = importlib.util.spec_from_loader("mypeople_authcheck", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _claude_authenticated():
    """A parseable credential file is NOT a login (card 8a6ebe4e48).

    `claude auth status` returns `loggedIn: true` for a credential whose access AND refresh tokens
    both expired -- proven against a hand-built dead credential, where the very next `claude -p`
    answered `Failed to authenticate. API Error: 401`. Trusting that flag is what made MyPlow
    announce `this node's claude login is active`, start the Boss, and paint the HUD ALIVE while
    the agent sat at "Not logged in". So the flag is now only the FIRST hurdle: the credential
    still has to survive authcheck (expiry stamps, then a real one-word probe).
    """
    if not shutil.which("claude"):
        return False
    slow = False
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=15)
        logged_in = False
        try:
            logged_in = bool(json.loads(r.stdout).get("loggedIn"))
        except Exception:
            normalized = "".join(ch for ch in (r.stdout + r.stderr).lower() if ch.isalnum())
            logged_in = "loggedintrue" in normalized or "loginmethod" in normalized
        if r.returncode != 0 or not logged_in:
            return False
    except subprocess.TimeoutExpired:
        # Slow is not logged out. `up` rewrites ~/.claude.json just before this gate, and with ~50
        # live sessions the next `auth status` took 23 s (6 s before the rewrite), so `make live`
        # refused a logged-in node twice (5.22.26, card d7679b9123). With no flag to go on, the
        # credential itself must prove live below.
        slow = True
    except Exception:
        return False
    try:
        state, detail = _authcheck().claude_verdict(timeout=90)
    except Exception:
        # authcheck itself is broken/absent -- fall back to the old (weaker) answer rather than
        # locking out a node that really is logged in.
        return not slow
    if state == "dead":
        _echo("[myplow] claude credential rejected: %s" % detail)
        return False
    return state == "live" or not slow


def _codex_authenticated():
    if shutil.which("codex"):
        try:
            r = subprocess.run(["codex", "login", "status"], capture_output=True,
                               text=True, timeout=15)
            return r.returncode == 0
        except Exception:
            pass
    return False


def _grok_authenticated():
    """grok has no `login status` verb, and `grok models` EXITS 0 EVEN WHEN LOGGED OUT --
    it just prints "You are not authenticated." and the anonymous model list. So the
    returncode idiom used for codex would pass an unauthenticated node; the affirmative
    stdout marker is the only honest signal. Fail closed on anything unrecognized."""
    if shutil.which("grok"):
        try:
            r = subprocess.run(["grok", "models"], capture_output=True,
                               text=True, timeout=25)
            out = (r.stdout + r.stderr).lower()
            if r.returncode == 0 and "you are logged in" in out:
                return True
        except Exception:
            pass
    return False


def authenticated_backends():
    """Every backend this node has its own completed login for, in VALID_BACKENDS order."""
    checks = {"claude": _claude_authenticated, "codex": _codex_authenticated,
              "grok": _grok_authenticated}
    return [b for b in VALID_BACKENDS if checks[b]()]


def resolve_auth(preferred=None, chooser=None):
    """Require this node's own completed login for the selected agent backend.

    With no explicit preference this used to return the first authenticated backend in
    VALID_BACKENDS order, so a machine that merely had Claude installed got Claude and was never
    asked -- reported as "since I have claude installed it used that for mypeople instead of
    chatgpt automatically" (card 5211716904, items 8+9). List order is an implementation detail,
    not a decision: when several logins are live, `chooser` decides, and if there is nobody to ask
    the pick is at least stated out loud along with how to override it.
    """
    preferred = (preferred or "").strip().lower()
    if preferred and preferred not in VALID_BACKENDS:
        return False, preferred, "Unknown backend %r; choose claude, codex or grok." % preferred
    checks = {"claude": _claude_authenticated, "codex": _codex_authenticated,
              "grok": _grok_authenticated}
    if preferred:
        if checks[preferred]():
            return True, preferred, "this node's %s login is active" % preferred
        available = []
    else:
        available = authenticated_backends()
    if available:
        if len(available) == 1:
            return True, available[0], "this node's %s login is active" % available[0]
        picked = (chooser(available) or "").strip().lower() if chooser else ""
        if picked in available:
            return True, picked, "using %s (chosen; %s also authenticated)" % (
                picked, ", ".join(b for b in available if b != picked))
        return True, available[0], (
            "%s are all authenticated; defaulting to %s. Re-run with `--backend <name>` to pick "
            "another." % (", ".join(available), available[0]))
    requested = preferred or "claude, codex or grok"
    return (False, preferred or "none",
            "This node is not authenticated for %s. Run `claude auth login`, `codex login` or "
            "`grok login` inside THIS node, then re-run MyPlow. Never copy or mount credentials "
            "from another node." % requested)


def auth_state_path(install=None):
    return os.path.join(install or install_dir(), "status", "auth.json")


def login_howto():
    """The exact commands that fix an unauthenticated node, phrased for where it is running.

    Written into the auth state file so the CLI, the board and the HUD all quote the same
    instruction instead of three copies drifting apart.
    """
    if os.environ.get("MYPEOPLE_DESKTOP") == "1":
        # The one-liner installs the AI its owner chose (Codex pinned, in ~/.local/bin), so a login is all that is left.
        return ["claude auth login",
                "or, for Codex: codex login"]
    if os.environ.get("MYPEOPLE_CONTAINER") == "1":
        return ["docker compose exec -it mypeople claude auth login",
                "(`codex login` / `grok login` work too)"]
    return ["claude auth login",
            "(`codex login` / `grok login` work too)",
            "then re-run `mypeople up`"]


def write_auth_state(install, authenticated, backend=None, message="", requested=""):
    """Publish this node's login state where the runtime can read it.

    The daemons run out of INSTALL_DIR under a bare interpreter with no route back to this
    package (same constraint that makes INSTALL_DIR/VERSION exist), so a file is how the pages
    and the boss supervisor learn whether this node can actually talk to an AI backend.
    """
    path = auth_state_path(install)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_json(path, {
            "authenticated": bool(authenticated),
            "backend": backend or "",
            "requested": requested or "",
            "message": message or "",
            "howto": [] if authenticated else login_howto(),
            "checked_at": int(time.time()),
        })
    except Exception:
        pass  # a status file must never be the reason the stack fails to come up


def _prompt_backend(available):
    """Ask which authenticated backend to run, when there is a human on the other end.

    A non-interactive install (docker build, CI, any piped stdin) must never block on a prompt, so
    this answers nothing and lets resolve_auth fall back to a stated default.
    """
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return ""
    except Exception:
        return ""
    _echo("\n[myplow] More than one AI backend is authenticated on this node:")
    for i, b in enumerate(available, 1):
        _echo("  %d) %s" % (i, b))
    try:
        raw = input("[myplow] Which should MyPlow use? [1-%d, default %s]: "
                    % (len(available), available[0])).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if not raw:
        return available[0]
    if raw.isdigit() and 1 <= int(raw) <= len(available):
        return available[int(raw) - 1]
    return raw.lower()


