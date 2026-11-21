# ---------------------------------------------------------------- step 5: tmux.conf
def install_tmux_conf(install):
    """Install MyPlow's tmux settings as an include while preserving the user's config."""
    dst = os.path.expanduser("~/.tmux.conf")
    src = os.path.join(install, "config", "tmux.conf")
    include = os.path.join(CONFIG_DIR, "tmux.runtime.conf")
    if not os.path.exists(src):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    shutil.copy2(src, include)
    directive = "source-file %s" % include
    existing = ""
    if os.path.exists(dst):
        try:
            existing = open(dst).read()
        except Exception:
            existing = ""
    if directive not in existing:
        with open(dst, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# MyPlow runtime settings\n%s\n" % directive)
    tpm = os.path.expanduser("~/.tmux/plugins/tpm")
    installer = os.path.join(tpm, "bin", "install_plugins")
    # The theme is cosmetic: a clone that hangs (it waited on the keychain in a fresh, headless
    # home) must cost the look, not the whole `up`, which died here before starting any daemon.
    try:
        if not os.path.isdir(tpm) and shutil.which("git"):
            subprocess.run(
                ["git", "clone", "--depth", "1", "https://github.com/tmux-plugins/tpm", tpm],
                capture_output=True,
                timeout=60,
            )
        if os.path.exists(installer):
            subprocess.run([installer], capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        _echo("[myplow] tmux plugins not installed (%s); continuing without the theme" % e)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "source-file", dst], capture_output=True)
