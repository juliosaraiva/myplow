# ---------- running version (single source: package __version__) ----------
# The runtime is copied out of the package into INSTALL_DIR and runs under a bare interpreter,
# so `import mypeople` is not reachable from here in a real install. firstrun.materialize()
# stamps INSTALL_DIR/VERSION from __version__; that file is this runtime's channel to it.
VERSION_UNKNOWN = "dev"


def live_drift():
    """Installed runtime files that no longer match what materialize stamped under VERSION
    (run/manifest.json): a hand copy or patch that skipped `make live`. [] when none, or no manifest."""
    install = CFG["INSTALL_DIR"]
    recovery = "run/runtime-install.pending"
    if os.path.isfile(os.path.join(install, recovery)):
        return [recovery]
    try:
        with open(os.path.join(install, "run", "manifest.json")) as f:
            files = json.load(f)["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    changed = []
    for rel, want in files.items():
        try:
            with open(os.path.join(install, rel), "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            got = None
        if got != want:
            changed.append(rel)
    return sorted(changed)


def version():
    """Version of the install actually serving this process. Never raises: a page must not
    500 over a version string."""
    env = os.environ.get("MYPEOPLE_VERSION")
    if env and env.strip():
        return env.strip()
    try:
        generation = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.basename(os.path.dirname(generation)) != ".runtime-generations":
            generation = CFG["INSTALL_DIR"]
        with open(os.path.join(generation, "VERSION")) as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    # running straight from the source tree / an editable install
    try:
        import mypeople

        return mypeople.__version__
    except Exception:
        pass
    return VERSION_UNKNOWN
