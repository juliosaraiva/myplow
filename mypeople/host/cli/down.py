# ---------------------------------------------------------------- down
def cmd_down(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    pidfile = os.path.join(install, "run", "supervise.pid")
    if os.path.exists(pidfile):
        try:
            pid = int(open(pidfile).read().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        try:
            os.remove(pidfile)
        except OSError:
            pass
    bindir = os.path.join(install, "bin")
    for pat in (
        "supervise.sh",
        "queue-server.py",
        "todo-server.py",
        "queue-client.py",
        "board-exporter.py",
        "boss-supervisor.sh",
        "mp cron serve",
        "mp tcc serve",
    ):
        subprocess.run(["pkill", "-f", os.path.join(bindir, pat)], capture_output=True)
    # Every plugin, not a named few: one `down` forgot kept serving its old code after an upgrade
    # while run/serving.version claimed the new release (latch-reconnect, 10-03).
    for sub in ("plugins", "plugins.d"):
        subprocess.run(["pkill", "-f", os.path.join(install, sub) + os.sep], capture_output=True)
    subprocess.run(["tmux", "kill-session", "-t", "discord-agent"], capture_output=True)
    # Writable + read-only ttyd. On Linux, supervise.sh detaches children with setsid so
    # killpg on the supervisor does not reach them. Without both pkill patterns, `down`/`up`
    # upgrades leave the RO graph-tile ttyd running with the old argv (card 789b5d04f9).
    # Empty string is a present key — cfg.get default only applies when MISSING.
    port = cfg.get("TTYD_PORT") or "7681"
    ro = cfg.get("TTYD_RO_PORT") or str(int(port) + 1)
    subprocess.run(["pkill", "-f", "ttyd -W -a -p %s" % port], capture_output=True)
    subprocess.run(["pkill", "-f", "ttyd -a -p %s" % ro], capture_output=True)
    print("[myplow] stopped (board/roster kept on disk in %s)" % install)
    return 0
