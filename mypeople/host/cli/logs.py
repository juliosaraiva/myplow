# ---------------------------------------------------------------- logs
def cmd_logs(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    logdir = os.path.join(install, "logs")
    name = args[0] if args else None
    import glob

    files = (
        [os.path.join(logdir, name + ".log")]
        if name
        else sorted(glob.glob(os.path.join(logdir, "*.log")) + glob.glob(os.path.join(logdir, "*.out")))
    )
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("[myplow] no logs in %s" % logdir)
        return 1
    return subprocess.call(["tail", "-n", "40", "-f"] + files)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-v", "--version"):
        print("mypeople %s" % __version__)
        return 0
    if not argv or argv[0] == "up":
        return cmd_up(argv[1:] if argv else [])
    verb, rest = argv[0], argv[1:]
    table = {
        "down": cmd_down,
        "status": cmd_status,
        "verify": cmd_verify,
        "logs": cmd_logs,
        "auth-check": cmd_auth_check,
    }
    fn = table.get(verb)
    if not fn:
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
