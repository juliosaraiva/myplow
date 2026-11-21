# ---------------------------------------------------------------- orchestration
def ensure(preferred_backend=None, allow_unauthenticated=None):
    """Idempotent first-run configuration. Returns (install, host, selected backend).

    `backend` comes back None when this node has no login yet AND exiting is not an option --
    see the login-required branch below.
    """
    install = install_dir()
    materialize(install)
    configured = load_cfg().get("DEFAULT_BACKEND")
    requested = (
        preferred_backend or os.environ.get("MYPEOPLE_BACKEND") or os.environ.get("DEFAULT_BACKEND") or configured
    )
    if allow_unauthenticated is None:
        # Only PID 1 under a restart policy, or the desktop app. An installer or an interactive
        # `mypeople up` still refuses cleanly and starts nothing -- that behaviour is the
        # reference, not the bug. The desktop app joins the container here because sys.exit(2)
        # in a double-clicked .app is a window that never opens and says nothing: it has no
        # terminal to print to, so the window it just refused to show is the only place to say
        # that a login is missing and which command to run in a terminal of the user's own.
        allow_unauthenticated = os.environ.get("MYPEOPLE_CONTAINER") == "1" or os.environ.get("MYPEOPLE_DESKTOP") == "1"
    # Backend config seeding happens BEFORE the auth gate on purpose (card 293fc81898). It writes
    # only local files and needs no login, but it is what pre-accepts Claude's first-run modals
    # (onboarding, folder trust, and the Bypass Permissions warning whose default button is "No,
    # exit"). Below the `sys.exit(2)` it never ran on a fresh install -- so the very first agent
    # spawned after the login landed sat on a modal, ate its prompt and died instead of running
    # with --dangerously-skip-permissions.
    write_claude_config(install)
    write_codex_config(install)
    ok, backend, msg = resolve_auth(requested, chooser=_prompt_backend)
    if not ok:
        _echo("\n[myplow] " + msg + "\n")
        if not allow_unauthenticated:
            sys.exit(2)
        # In a container this process IS the container. Exiting here hands `restart:
        # unless-stopped` a process whose only possible outcome is the same exit code, so the
        # user gets an endless restart loop and dead ports instead of a login prompt (card
        # f0e7101c94). Come up degraded instead: the board and the HUD serve, they say what is
        # missing, and the boss supervisor starts the Boss by itself once the login lands.
        _echo("[myplow] LOGIN REQUIRED — starting board + HUD only; no agent can run yet.")
        backend = None
    else:
        _echo("[myplow] auth: %s" % msg)
    fresh = write_queue_env(install, backend or requested or "claude")
    install_tmux_conf(install)
    write_auth_state(install, ok, backend, msg, requested or "")
    host = os.environ.get("HOST_ID") or load_cfg().get("HOST_ID") or socket.gethostname().split(".")[0]
    _echo("[myplow] install dir: %s  (config: %s%s)" % (install, CONFIG_PATH, ", fresh" if fresh else ", reused"))
    return install, host, backend
