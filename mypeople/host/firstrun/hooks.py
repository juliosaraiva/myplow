# ---------------------------------------------------------------- step 4: backend hooks
_HOOK_TAIL = "/plugins/tmux-boss-hooks/emit-event.sh"


def _mypeople_hook_group(group):
    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
        command = handler.get("command", "") if isinstance(handler, dict) else ""
        if _HOOK_TAIL in command:
            return True
    return False


def daemons_running(install):
    """True while INSTALL's daemons are up."""
    pat = os.path.join(install, "bin", "todo-server.py")
    return subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0


def _hook_installs(hooks):
    """The install dirs whose emit-event.sh the MyPlow hooks in HOOKS call."""
    found = set()
    for groups in hooks.values():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []) if _mypeople_hook_group(group) else []:
                try:
                    script = shlex.split(handler.get("command", ""))[0]
                except (ValueError, IndexError, AttributeError):
                    continue
                if script.endswith(_HOOK_TAIL):
                    found.add(script[:-len(_HOOK_TAIL)])
    return found


def _replace_mypeople_hooks(hooks, hook):
    """Remove every prior MyPlow hook, including retired events, then install current ones.

    Unless they belong to another install that is RUNNING: ~/.claude and ~/.codex serve every agent
    on the machine, so a second install (a smoke test, a stray app with its own INSTALL_DIR) would
    aim every live agent's hooks at itself, and at nothing once it is deleted (card 8f490e73e5).
    """
    if not isinstance(hooks, dict):
        hooks = {}
    mine = os.path.realpath(hook[:-len(_HOOK_TAIL)])
    held = sorted(i for i in _hook_installs(hooks) if os.path.realpath(i) != mine and daemons_running(i))
    if held:
        _echo("[myplow] agent hooks belong to the running install at %s; leaving them" % held[0])
        return hooks
    for event in list(hooks):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _mypeople_hook_group(group)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    for event in LIFECYCLE_EVENTS:
        command = "%s %s" % (shlex.quote(hook), event)
        hooks.setdefault(event, []).append({"hooks": [
            {"type": "command", "command": command}
        ]})
    return hooks


def write_claude_config(install):
    """Claude-Code onboarding + folder-trust + the Boss lifecycle hook, pointed at THIS
    install's plugin path. Replaces seed Step 3."""
    # ~/.claude.json — onboarding + trust for install dir and the agent cwds
    cj = os.path.expanduser("~/.claude.json")
    d = {}
    if os.path.exists(cj):
        try:
            d = json.load(open(cj)) or {}
        except Exception:
            d = {}
    if not isinstance(d, dict):
        d = {}
    d["hasCompletedOnboarding"] = True
    proj = d.setdefault("projects", {})
    for path in {os.path.expanduser("~"), install, os.path.join(install, "run"),
                 os.path.join(install, "run", "boss"), os.path.join(install, "run", "eng"),
                 os.path.join(install, "bin")}:
        rec = proj.get(path, {})
        rec["hasTrustDialogAccepted"] = True
        proj[path] = rec
    _atomic_json(cj, d)

    # ~/.claude/settings.json — shared lifecycle hooks + dangerous-mode precondition
    cs_dir = os.path.expanduser("~/.claude")
    os.makedirs(cs_dir, exist_ok=True)
    hook = os.path.join(install, "plugins", "tmux-boss-hooks", "emit-event.sh")
    settings_path = os.path.join(cs_dir, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f) or {}
        except Exception:
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings["hooks"] = _replace_mypeople_hooks(settings.get("hooks", {}), hook)
    settings["skipDangerousModePermissionPrompt"] = True
    _atomic_json(settings_path, settings)


def write_codex_config(install):
    """Install the same lifecycle contract for Codex without touching unrelated hooks."""
    codex_dir = os.path.expanduser("~/.codex")
    os.makedirs(codex_dir, exist_ok=True)
    hooks_path = os.path.join(codex_dir, "hooks.json")
    config = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path) as f:
                config = json.load(f) or {}
        except Exception:
            config = {}
    if not isinstance(config, dict):
        config = {}
    hook = os.path.join(install, "plugins", "tmux-boss-hooks", "emit-event.sh")
    config["hooks"] = _replace_mypeople_hooks(config.get("hooks", {}), hook)
    _atomic_json(hooks_path, config)


def _atomic_json(path, obj):
    """Write `obj` to `path` atomically, and NOT AT ALL when the file already holds it.

    ~/.claude.json and ~/.claude/settings.json are live files for every running Claude session.
    `up` rewrote both on every run with nothing changed: under ~50 sessions the next `claude auth
    status` took 23 s (card d7679b9123), and a session's own write landing between our read and
    our replace was lost."""
    try:
        with open(path) as f:
            if json.load(f) == obj:
                return
    except (OSError, ValueError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


