# ---------------------------------------------------------------- auth-check
def _another_home(install):
    """Is MYPEOPLE_HOME naming an install while this user's HOME is overridden to somewhere that does not hold it?"""
    if not os.environ.get("MYPEOPLE_HOME"):
        return False
    try:
        import pwd

        own = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):  # a host with no passwd (the Windows port): HOME is all it has
        return False
    home = os.path.realpath(os.path.expanduser("~"))
    return home != os.path.realpath(own) and not os.path.realpath(install).startswith(home + os.sep)


def cmd_auth_check(args):
    """Re-resolve this node's login and republish the auth state file.

    Called on a loop by boss-supervisor.sh so a login performed while the stack is already
    running takes effect without a down/up cycle. Exits 0 when this node can run agents.
    """
    install = firstrun.install_dir()
    state = {}
    try:
        with open(firstrun.auth_state_path(install)) as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    # Honour an explicit backend choice; with none, ANY completed login unblocks the node --
    # the user must not be forced into claude just because it is the default name in the config.
    requested = (state.get("requested") or "").strip() or None
    ok, backend, msg = firstrun.resolve_auth(requested, chooser=None)
    if _another_home(install):
        # A clean-room check: HOME points elsewhere while MYPEOPLE_HOME, which every agent's shell carries, names a real install.
        # The answer is about that HOME. Published, it told the owner's board he was logged out when he was not (11-05).
        return 0 if ok else 1
    firstrun.write_auth_state(install, ok, backend if ok else None, msg, requested or "")
    if ok:
        # Persist the backend the login actually landed on, so every later spawn agrees with it.
        if (load_cfg().get("DEFAULT_BACKEND") or "") != backend:
            firstrun.write_queue_env(install, backend)
        # This is the last thing that runs before boss-supervisor spawns the Boss, and on a node
        # that came up degraded it may be the first pass that ever completes. Re-seed the backend
        # first-run config here so the Boss cannot be launched into an unaccepted Bypass
        # Permissions modal (card 293fc81898). Both writers are idempotent merges.
        try:
            firstrun.write_claude_config(install)
            firstrun.write_codex_config(install)
        except Exception as e:
            print("[myplow] warning: could not seed backend config: %s" % e, file=sys.stderr)
        if "--quiet" not in args:
            print("[myplow] auth: %s" % msg)
        return 0
    if "--quiet" not in args:
        print("[myplow] %s" % msg, file=sys.stderr)
    return 1
