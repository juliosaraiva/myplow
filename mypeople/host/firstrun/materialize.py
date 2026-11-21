# ---------------------------------------------------------------- step 1: materialize
def _replace_file(src, dst):
    """copytree's default copy2 writes THROUGH the destination, which fails on an install that
    mprole has already published to: the role store is chmod 444 so a runtime agent cannot rewrite
    its own personality. Replace the file and put that mode back, so upgrading an install that has
    ever spawned a role doesn't die half-way with EACCES."""
    mode = None
    if os.path.lexists(dst):
        try:
            mode = stat.S_IMODE(os.lstat(dst).st_mode)
        except OSError:
            mode = None
        try:
            os.unlink(dst)
        except OSError:
            pass
    shutil.copy2(src, dst)
    if mode is not None and not mode & stat.S_IWUSR:
        try:
            os.chmod(dst, mode)
        except OSError:
            pass


def _write_file(dst, text):
    """Same replace-don't-write-through discipline as _replace_file, for content we generate
    rather than copy."""
    try:
        os.unlink(dst)
    except OSError:
        pass
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)


def _read_registry_roles(path):
    """The roles map of an existing install's registry, or {} if there is none to keep."""
    try:
        with open(path, encoding="utf-8") as f:
            roles = json.load(f).get("roles")
        return roles if isinstance(roles, dict) else {}
    except (OSError, ValueError):
        return {}


def _version_tuple(text):
    """(5, 13, 1) from "5.13.1". None when it is not a plain numeric version."""
    parts = (text or "").strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def installed_version(install):
    try:
        with open(os.path.join(install, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""


def refuse_downgrade(install):
    """True when INSTALL_DIR already holds a NEWER release than this package carries.

    Materialize overwrites the install with whatever the caller ships, and the desktop app
    carries its own copy of the runtime. So an old .app -- one left in a build folder, or simply
    reopened by macOS after a reboot -- silently rolled a 5.13.1 install back to 5.8.0 twice on
    the CEO's Mac, taking every feature since with it and breaking message delivery. Nothing here
    can tell an intended downgrade from an accident, so the older side always yields.

    Set MYPEOPLE_ALLOW_DOWNGRADE=1 to force it (a deliberate rollback still has a way through).
    """
    if os.environ.get("MYPEOPLE_ALLOW_DOWNGRADE") == "1":
        return False
    from . import __version__
    here, there = _version_tuple(__version__), _version_tuple(installed_version(install))
    return bool(here and there and there > here)


def materialize(install):
    """Copy the packaged runtime into a WRITABLE INSTALL_DIR. Idempotent: never overwrite
    existing daemon code differently, and NEVER clobber live state (board/roster/logs)."""
    from . import __version__
    if refuse_downgrade(install):
        _echo("[myplow] this build is %s and %s already runs %s — leaving it alone.\n"
              "[myplow] open the current app, or set MYPEOPLE_ALLOW_DOWNGRADE=1 to force it."
              % (__version__, install, installed_version(install)))
        return
    rt = runtime_dir()
    os.makedirs(install, exist_ok=True)
    # code trees: refresh from the package so upgrades take effect (state dirs excluded below).
    # "roles" carries the versioned role store (personalities + Skills + profiles). mp resolves
    # every --role spawn against INSTALL_DIR/roles and fails closed if it is absent, so omitting
    # it here would ship a runtime whose Boss cannot be born.
    # roles/registry.json is the spawn allowlist, and an install can hold roles the product does
    # not ship -- anything the creator authored here. The copy below would replace that file with
    # the shipped one, silently unregistering every authored role (they stay on disk but become
    # unspawnable). So keep the entries this release does not name; shipped names still move
    # forward to the versions this release carries.
    kept_roles = _read_registry_roles(os.path.join(install, "roles", "registry.json"))
    # Bytecode never travels with the code, and none may stay. The app compiles its bundled runtime
    # as unchecked-hash .pyc (5.21.31, so the signed bundle stays sealed), which Python never checks
    # against the source: copied in here, they kept every daemon on the app's mpcommon after 5.22.0
    # replaced mpcommon.py beside them. Imports rebuild a cache from the .py that is actually here.
    for sub in ("bin", "lib", "plugins", "verify", "config", "roles"):
        src = os.path.join(rt, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(install, sub), dirs_exist_ok=True,
                            copy_function=_replace_file,
                            ignore=shutil.ignore_patterns("__pycache__"))
        for root, dirs, _ in os.walk(os.path.join(install, sub)):
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
                shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
    if kept_roles:
        reg_path = os.path.join(install, "roles", "registry.json")
        with open(reg_path, encoding="utf-8") as f:
            reg = json.load(f)
        mode = stat.S_IMODE(os.stat(reg_path).st_mode)
        merged = dict(kept_roles)
        merged.update(reg.get("roles") or {})
        # A shipped name this install points at a profile of its OWN (a factory agent's Boss with the
        # person's role, 10-18) stays pointed there: the product's version only replaces the product's
        # versions. Moving it to the stock profile made every restart revive that Boss onto the wrong
        # role, or refuse it on a locked-role drift, so the agent stayed down.
        merged.update({r: ref for r, ref in kept_roles.items()
                       if not os.path.isfile(os.path.join(rt, "roles", *ref.split("/")))})
        # a kept entry whose profile this release deleted would fail closed at spawn; drop it.
        reg["roles"] = {r: ref for r, ref in merged.items()
                        if os.path.isfile(os.path.join(install, "roles", *ref.split("/")))}
        _write_file(reg_path, json.dumps(reg, indent=1) + "\n")
        os.chmod(reg_path, mode)   # the store is published read-only; keep it that way
    # Files a previous version shipped and this one does not. copytree(dirs_exist_ok) only ever
    # adds, so a retired page would survive every upgrade forever as a file the product no longer
    # serves. Removing it here is what makes "removed" true on an upgraded install, not just a
    # fresh one.
    for gone in ("bin/wall.html", "boss-CLAUDE.md", "plans/boss-claude.md"):
        try:
            os.remove(os.path.join(install, *gone.split("/")))
        except OSError:
            pass
    # Same for plugins, generally: a plugin folder this release does not ship goes whole. Removing
    # only its files left latch-reconnect's folder behind (10-29). A source folder holding nothing
    # but bytecode, what `git rm` leaves in a checkout, ships nothing.
    src = os.path.join(rt, "plugins")
    shipped = {name for name in os.listdir(src)
               if any(files for root, _, files in os.walk(os.path.join(src, name)) if "__pycache__" not in root)}
    for name in os.listdir(os.path.join(install, "plugins")):
        if name not in shipped and os.path.isdir(os.path.join(install, "plugins", name)):
            shutil.rmtree(os.path.join(install, "plugins", name), ignore_errors=True)
    # Boss doctrine file
    bc = os.path.join(rt, "mp-boss-doctrine.md")
    if os.path.exists(bc):
        _replace_file(bc, os.path.join(install, "mp-boss-doctrine.md"))
    # Stamp the version this runtime was materialized from. The daemons run the copies above
    # under a bare interpreter with no route back to the package, so this file is how they (and
    # the UI) know which version is serving. Rewritten here so an upgrade cannot leave a stale
    # number on the pages; a hand-placed VERSION is replaced the same way the code trees are.
    from . import __version__
    _write_file(os.path.join(install, "VERSION"), __version__ + "\n")
    # writable state skeletons — create empty, never overwrite existing board/roster/logs
    for sub in ("todos", "run", "status", "logs"):
        os.makedirs(os.path.join(install, sub), exist_ok=True)
    # Fingerprint every file just installed under that VERSION, so a later hand copy or patch
    # cannot hide behind it: `mp status` names any file that no longer matches (card 8f490e73e5).
    # roles/registry.json is merged above and rewritten by role publishing, so it is not code we own.
    files = {}
    for sub in ("bin", "lib", "plugins", "verify", "config", "roles"):
        for root, dirs, names in os.walk(os.path.join(rt, sub)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            for name in names:
                rel = os.path.relpath(os.path.join(root, name), rt)
                dst = os.path.join(install, rel)
                if rel != os.path.join("roles", "registry.json") and os.path.isfile(dst):
                    with open(dst, "rb") as f:
                        files[rel] = hashlib.sha256(f.read()).hexdigest()
    _write_file(os.path.join(install, "run", "manifest.json"),
                json.dumps({"version": __version__, "files": files}, indent=1, sort_keys=True) + "\n")
    # make scripts executable (package-data can lose the bit on some backends)
    bindir = os.path.join(install, "bin")
    for f in os.listdir(bindir):
        p = os.path.join(bindir, f)
        if f.endswith((".py", ".sh")) or f in ("mp", "board-restore"):
            try:
                os.chmod(p, 0o755)
            except OSError:
                pass
    for f in ("emit-event.sh",):
        p = os.path.join(install, "plugins", "tmux-boss-hooks", f)
        if os.path.exists(p):
            os.chmod(p, 0o755)
    vs = os.path.join(install, "verify", "verify.sh")
    if os.path.exists(vs):
        os.chmod(vs, 0o755)


