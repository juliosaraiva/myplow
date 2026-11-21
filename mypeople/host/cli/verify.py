# ---------------------------------------------------------------- verify
def cmd_verify(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    vs = os.path.join(install, "verify", "verify.sh")
    if not os.path.exists(vs):
        print("[myplow] verify.sh missing; run `mypeople up` first", file=sys.stderr)
        return 1
    env = child_env(cfg)
    return subprocess.call(["bash", vs], env=env, cwd=install)
