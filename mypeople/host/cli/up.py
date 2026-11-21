# ---------------------------------------------------------------- up
def cmd_up(args):
    role = "both"
    if "--server" in args:
        role = "server"
    if "--client" in args:
        role = "client"
    detach = "--detach" in args
    foreground = not detach
    backend = None
    if "--backend" in args:
        idx = args.index("--backend")
        if idx + 1 >= len(args):
            print("[myplow] --backend needs claude, codex or grok", file=sys.stderr)
            return 2
        backend = args[idx + 1]

    install, host, backend = firstrun.ensure(backend)
    cfg = load_cfg()
    env = child_env(cfg)
    bindir = os.path.join(install, "bin")

    if role == "client":
        return _up_client(cfg, env, bindir, foreground)

    # server / both: the existing supervisor starts every daemon + boss-supervisor
    restart_if_serving_stale(cfg, install)
    log = os.path.join(install, "logs", "supervise.out")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "ab") as lf:
        supervisor = subprocess.Popen(["bash", os.path.join(bindir, "supervise.sh")],
                                      env=env, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                      start_new_session=True, cwd=install)
    _stamp_serving_version(install)
    print("[myplow] daemons starting (role=%s) ..." % role)
    if not wait_health(cfg):
        print("[myplow] WARNING: HUD health not ready after 40s; check `mypeople logs`",
              file=sys.stderr)
    if backend is None:
        # Login-required mode: the front doors are up and say so, but there is no backend to
        # spawn an agent against yet. boss-supervisor.sh re-checks and starts the Boss on its
        # own the moment a login lands, so the user never needs a down/up cycle.
        print_login_required(cfg)
    else:
        # Ensure the Boss deterministically. Existing nodes must resume their persisted session;
        # only a node without a Boss roster entry may create the initial session.
        mp = os.path.join(bindir, "mp")
        subprocess.run(["python3", mp, "ensure-boss", "%s/main:Boss" % host,
                        "--backend", backend],
                       env=env, cwd=install)
    print_urls(cfg)
    if foreground:
        if os.environ.get("MYPEOPLE_CONTAINER") == "1":
            try:
                return supervisor.wait()
            except KeyboardInterrupt:
                supervisor.terminate()
                return 130
        _follow(install, cfg)
    return 0


def _up_client(cfg, env, bindir, foreground):
    up = os.environ.get("UPSTREAM_QUEUE_URL") or cfg.get("UPSTREAM_QUEUE_URL")
    if not up:
        print("[myplow] client mode needs UPSTREAM_QUEUE_URL=http://<server-host>:9900",
              file=sys.stderr)
        return 2
    upstream_secret = os.environ.get("UPSTREAM_QUEUE_SECRET") or cfg.get("UPSTREAM_QUEUE_SECRET")
    if not upstream_secret:
        print("[myplow] client mode needs UPSTREAM_QUEUE_SECRET", file=sys.stderr)
        return 2
    # queue-client consumes QUEUE_URL/QUEUE_SECRET. Override both for this isolated client process.
    env["UPSTREAM_QUEUE_URL"] = up
    env["QUEUE_URL"] = up
    env["QUEUE_SECRET"] = upstream_secret
    print("[myplow] client → %s" % up)
    p = subprocess.Popen(["python3", os.path.join(bindir, "queue-client.py")], env=env)
    if foreground:
        try:
            p.wait()
        except KeyboardInterrupt:
            p.terminate()
    return 0


def _follow(install, cfg):
    print("[myplow] following — Ctrl-C to detach (daemons keep running; `mypeople down` to stop)\n")
    daemon_log = os.path.join(install, "logs", "daemon.log")
    try:
        open(daemon_log, "a").close()
        p = subprocess.Popen(["tail", "-n", "0", "-f", daemon_log])
        p.wait()
    except KeyboardInterrupt:
        try:
            p.terminate()
        except Exception:
            pass
        print("\n[myplow] detached. `mypeople status` / `mypeople down`.")


