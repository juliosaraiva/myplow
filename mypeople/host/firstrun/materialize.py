# ---------------------------------------------------------------- step 1: materialize
import ast
import fcntl
import tempfile

_CODE_TREES = ("bin", "lib", "plugins", "verify", "config", "roles")
_RUNTIME_CURRENT = ".runtime-current"
_RUNTIME_GENERATIONS = ".runtime-generations"
_RUNTIME_PENDING = "runtime-install.pending"
_RUNTIME_LAUNCHER = """#!/usr/bin/env python3
# Stable installed launcher. Resolve the generation once before opening any code.
def _run_installed():
    import os
    import sys

    namespace = globals()
    front = os.path.abspath(namespace["__file__"])
    install = os.path.dirname(os.path.dirname(front))
    current = os.path.realpath(os.path.join(install, ".runtime-current"))
    entrypoint = os.path.join(current, "bin", os.path.basename(front))
    if not os.path.isfile(entrypoint):
        print(f"{os.path.basename(front)} is unavailable in the active runtime", file=sys.stderr)
        raise SystemExit(127)
    namespace["__file__"] = entrypoint
    sys.path.insert(0, os.path.dirname(entrypoint))
    sys.dont_write_bytecode = True
    with open(entrypoint, "rb") as source:
        exec(compile(source.read(), entrypoint, "exec"), namespace)

_run_installed()
del _run_installed
"""


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_file(src, dst):
    """Replace one complete file without writing through a published role or symlink."""
    folder = os.path.dirname(dst)
    fd, temp = tempfile.mkstemp(dir=folder)
    os.close(fd)
    try:
        shutil.copy2(src, temp)
        if os.path.isfile(dst) and not os.path.islink(dst):
            mode = stat.S_IMODE(os.stat(dst).st_mode)
            if not mode & stat.S_IWUSR:
                os.chmod(temp, mode)
        with open(temp, "rb") as file:
            os.fsync(file.fileno())
        os.replace(temp, dst)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _write_file(dst, text, mode=0o644):
    folder = os.path.dirname(dst)
    fd, temp = tempfile.mkstemp(dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            os.fchmod(file.fileno(), mode)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, dst)
        _sync_dir(folder)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _is_python_entry(name):
    return name.endswith(".py") or name in ("mp", "board-restore")


def _walk_runtime(root, trees):
    for sub in trees:
        for folder, dirs, names in os.walk(os.path.join(root, sub)):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "node_modules"))
            for name in sorted(names):
                path = os.path.join(folder, name)
                if not name.endswith(".pyc") and os.path.isfile(path):
                    yield os.path.relpath(path, root)


def _runtime_hashes(root, trees):
    result = {}
    for rel in _walk_runtime(root, trees):
        with open(os.path.join(root, rel), "rb") as file:
            result[rel] = hashlib.sha256(file.read()).hexdigest()
    return result


def _validate_generation(stage):
    """Check the static fragment references used by shipped Python loaders."""
    for name in os.listdir(os.path.join(stage, "bin")):
        path = os.path.join(stage, "bin", name)
        if not _is_python_entry(name) or not os.path.isfile(path):
            continue
        with open(path, "rb") as file:
            syntax = ast.parse(file.read(), filename=path)
        for node in syntax.body:
            if not isinstance(node, ast.FunctionDef) or node.name != "_load_implementation":
                continue
            strings = [
                item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            folder = next((item for item in strings if item == "../lib" or item.startswith("../lib/")), None)
            if folder is None:
                continue
            for fragment in (item for item in strings if item.endswith(".py")):
                path = os.path.join(stage, "bin", folder, fragment)
                with open(path, "rb") as file:
                    compile(file.read(), path, "exec")


def _python_entries(root):
    bindir = os.path.join(root, "bin")
    return {
        name for name in os.listdir(bindir) if _is_python_entry(name) and os.path.isfile(os.path.join(bindir, name))
    }


def _stage_generation(source, install, version, *, legacy=False):
    root = os.path.join(install, _RUNTIME_GENERATIONS)
    os.makedirs(root, exist_ok=True)
    trees = ("bin", "lib") if legacy else _CODE_TREES
    identity = json.dumps([legacy, version, _runtime_hashes(source, trees)], sort_keys=True).encode()
    generation = os.path.join(root, hashlib.sha256(identity).hexdigest())
    if os.path.isdir(generation):
        return generation
    with tempfile.TemporaryDirectory(dir=root, prefix=".stage-") as stage:
        for sub in ("bin", "lib"):
            src = os.path.join(source, sub)
            if os.path.isdir(src):
                shutil.copytree(
                    src,
                    os.path.join(stage, sub),
                    copy_function=_replace_file,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
                )
        _validate_generation(stage)
        fronts = os.path.join(stage, "launchers")
        os.mkdir(fronts)
        for name in sorted(_python_entries(stage)):
            _write_file(os.path.join(fronts, name), _RUNTIME_LAUNCHER, 0o755)
        _write_file(os.path.join(stage, "VERSION"), version + "\n")
        if legacy:
            old_manifest = os.path.join(source, "run", "manifest.json")
            if os.path.isfile(old_manifest):
                _replace_file(old_manifest, os.path.join(stage, "manifest.json"))
        for directory, _, _ in os.walk(stage, topdown=False):
            _sync_dir(directory)
        os.rename(stage, generation)
        _sync_dir(root)
    return generation


def _atomic_link(dst, target):
    pending_link = dst + ".next"
    if os.path.lexists(pending_link):
        os.unlink(pending_link)
    os.symlink(target, pending_link)
    os.replace(pending_link, dst)
    _sync_dir(os.path.dirname(dst))


def _activate_generation(install, generation):
    _atomic_link(os.path.join(install, _RUNTIME_CURRENT), os.path.relpath(generation, install))


def _install_launchers(install, generation):
    active = _python_entries(os.path.realpath(os.path.join(install, _RUNTIME_CURRENT)))
    new = _python_entries(generation)
    for name in sorted(active | new):
        dst = os.path.join(install, "bin", name)
        if name in active and name in new and not os.path.islink(dst):
            _write_file(dst, _RUNTIME_LAUNCHER, 0o755)
        else:
            # An added entry is dormant before activation, and a retired one goes
            # dormant upon activation. The same link resumes working on rollback.
            target = os.path.join("..", _RUNTIME_CURRENT, "launchers", name)
            _atomic_link(dst, target)


def _publish_metadata(install, generation):
    with open(os.path.join(generation, "VERSION"), encoding="utf-8") as file:
        version = file.read()
    _write_file(os.path.join(install, "VERSION"), version)
    with open(os.path.join(generation, "manifest.json"), encoding="utf-8") as file:
        manifest = file.read()
    _write_file(os.path.join(install, "run", "manifest.json"), manifest)


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
        path = "VERSION"
        if os.path.isfile(os.path.join(install, "run", _RUNTIME_PENDING)) and os.path.isfile(
            os.path.join(install, _RUNTIME_CURRENT, "VERSION")
        ):
            path = os.path.join(_RUNTIME_CURRENT, "VERSION")
        with open(os.path.join(install, path)) as f:
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
    """Install a complete generation under the installer lock, preserving writable state."""
    from . import __version__

    install = os.path.abspath(install)
    os.makedirs(install, exist_ok=True)
    with open(os.path.join(install, ".runtime-install.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if refuse_downgrade(install):
            _echo(
                "[myplow] this build is %s and %s already runs %s — leaving it alone.\n"
                "[myplow] open the current app, or set MYPEOPLE_ALLOW_DOWNGRADE=1 to force it."
                % (__version__, install, installed_version(install))
            )
            return
        _materialize_locked(install, runtime_dir(), __version__)


def _ignore_code_files(source, tree):
    patterns = ("__pycache__", "*.pyc")
    if tree == "bin":
        patterns += ("*.py", "mp", "board-restore")
    ignore = shutil.ignore_patterns(*patterns)

    def filter_entries(folder, names):
        result = ignore(folder, names)
        if tree == "roles" and folder == source:
            result.add("registry.json")
        return result

    return filter_entries


def _materialize_locked(install, rt, version):
    generation = _stage_generation(rt, install, version)
    rundir = os.path.join(install, "run")
    os.makedirs(rundir, exist_ok=True)
    pending = os.path.join(rundir, _RUNTIME_PENDING)
    _write_file(pending, json.dumps({"target": os.path.basename(generation)}) + "\n")
    current = os.path.join(install, _RUNTIME_CURRENT)
    if not os.path.isdir(current):
        # Keep even an old split install usable while replacing its front launchers.
        # Never rewrite the old loose lib tree: an older process may still read it.
        old_mp = os.path.join(install, "bin", "mp")
        initial = (
            _stage_generation(install, install, installed_version(install), legacy=True)
            if os.path.isfile(old_mp)
            else generation
        )
        _activate_generation(install, initial)
    lib = os.path.join(install, "lib")
    if not os.path.lexists(lib) and os.path.isdir(os.path.join(generation, "lib")):
        # Retain a separate loose tree for installers and readers predating generations.
        # A compatibility link into managed code would let an older installer write through it.
        with tempfile.TemporaryDirectory(dir=install, prefix=".lib-stage-") as stage:
            copy = os.path.join(stage, "lib")
            shutil.copytree(os.path.join(generation, "lib"), copy, copy_function=_replace_file)
            os.rename(copy, lib)
            _sync_dir(install)
    # code trees: refresh from the package so upgrades take effect (state dirs excluded below).
    # "roles" carries the versioned role store (personalities + Skills + profiles). mp resolves
    # every --role spawn against INSTALL_DIR/roles and fails closed if it is absent, so omitting
    # it here would ship a runtime whose Boss cannot be born.
    # roles/registry.json is the spawn allowlist, and an install can hold roles the product does
    # not ship -- anything the creator authored here. The copy below would replace that file with
    # the shipped one, silently unregistering every authored role (they stay on disk but become
    # unspawnable). So keep the entries this release does not name; shipped names still move
    # forward to the versions this release carries.
    reg_path = os.path.join(install, "roles", "registry.json")
    kept_roles = _read_registry_roles(reg_path)
    # Bytecode never travels with the code, and none may stay. The app compiles its bundled runtime
    # as unchecked-hash .pyc (5.21.31, so the signed bundle stays sealed), which Python never checks
    # against the source: copied in here, they kept every daemon on the app's mpcommon after 5.22.0
    # replaced mpcommon.py beside them. Imports rebuild a cache from the .py that is actually here.
    for sub in ("bin", "plugins", "verify", "config", "roles"):
        src = os.path.join(rt, sub)
        if os.path.isdir(src):
            shutil.copytree(
                src,
                os.path.join(install, sub),
                dirs_exist_ok=True,
                copy_function=_replace_file,
                ignore=_ignore_code_files(src, sub),
            )
        for root, dirs, _ in os.walk(os.path.join(install, sub)):
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
                shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
    shipped_registry = os.path.join(rt, "roles", "registry.json")
    if os.path.isfile(shipped_registry):
        with open(shipped_registry, encoding="utf-8") as f:
            reg = json.load(f)
        mode_path = reg_path if os.path.isfile(reg_path) else shipped_registry
        mode = stat.S_IMODE(os.stat(mode_path).st_mode)
        merged = dict(kept_roles)
        merged.update(reg.get("roles") or {})
        # A shipped name this install points at a profile of its OWN (a factory agent's Boss with the
        # person's role, 10-18) stays pointed there: the product's version only replaces the product's
        # versions. Moving it to the stock profile made every restart revive that Boss onto the wrong
        # role, or refuse it on a locked-role drift, so the agent stayed down.
        merged.update(
            {r: ref for r, ref in kept_roles.items() if not os.path.isfile(os.path.join(rt, "roles", *ref.split("/")))}
        )
        # a kept entry whose profile this release deleted would fail closed at spawn; drop it.
        reg["roles"] = {
            r: ref for r, ref in merged.items() if os.path.isfile(os.path.join(install, "roles", *ref.split("/")))
        }
        # Publish only the final registry. A SIGKILL can leave either complete
        # version on disk, and both preserve existing custom registrations.
        _write_file(reg_path, json.dumps(reg, indent=1) + "\n", mode)
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
    shipped = {
        name
        for name in os.listdir(src)
        if any(files for root, _, files in os.walk(os.path.join(src, name)) if "__pycache__" not in root)
    }
    for name in os.listdir(os.path.join(install, "plugins")):
        if name not in shipped and os.path.isdir(os.path.join(install, "plugins", name)):
            shutil.rmtree(os.path.join(install, "plugins", name), ignore_errors=True)
    # Boss doctrine file
    bc = os.path.join(rt, "mp-boss-doctrine.md")
    if os.path.exists(bc):
        _replace_file(bc, os.path.join(install, "mp-boss-doctrine.md"))
    _install_launchers(install, generation)
    for sub in ("todos", "run", "status", "logs"):
        os.makedirs(os.path.join(install, sub), exist_ok=True)
    files = {}
    for rel in _walk_runtime(rt, ("bin", "plugins", "verify", "config", "roles")):
        dst = os.path.join(install, rel)
        if rel.startswith("bin" + os.sep) and _is_python_entry(os.path.basename(rel)):
            dst = os.path.join(generation, "launchers", os.path.basename(rel))
        if rel != os.path.join("roles", "registry.json") and os.path.isfile(dst):
            with open(dst, "rb") as file:
                files[rel] = hashlib.sha256(file.read()).hexdigest()
    prefix = os.path.relpath(generation, install)
    files.update(
        {
            os.path.join(prefix, rel): digest
            for rel, digest in _runtime_hashes(generation, ("bin", "lib", "launchers")).items()
        }
    )
    manifest = (
        json.dumps({"version": version, "runtime_generation": prefix, "files": files}, indent=1, sort_keys=True) + "\n"
    )
    meta = os.path.join(generation, "manifest.json")
    if not os.path.isfile(meta):
        _write_file(meta, manifest)
    # make scripts executable (package-data can lose the bit on some backends)
    bindir = os.path.join(install, "bin")
    for f in os.listdir(bindir):
        p = os.path.join(bindir, f)
        if not os.path.islink(p) and (f.endswith((".py", ".sh")) or f in ("mp", "board-restore")):
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
    _activate_generation(install, generation)
    _publish_metadata(install, generation)
    os.unlink(pending)
    _sync_dir(rundir)
