# ---------------------------------------------------------------- step 3: queue.env
def _write_env_file(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_queue_env(install, backend):
    """Write the single config file if absent. Idempotent — reuse keeps the same
    QUEUE_SECRET/HOST_ID across restarts so board/sessions persist."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            lines = f.read().splitlines()
        existing = load_cfg()
        # claude-opus-4-8 was the shipped default until 5.22.122: a file still on it never chose it (#49).
        current = lambda m: "claude-opus-5-5" if m in ("", "claude-opus-4-8") else m
        replacements = {
            "DEFAULT_BACKEND": backend,
            "DEFAULT_ENG_MODEL": current(existing.get("DEFAULT_ENG_MODEL", "")),
            "DEFAULT_CLAUDE_MODEL": current(
                os.environ.get(
                    "DEFAULT_CLAUDE_MODEL", existing.get("DEFAULT_CLAUDE_MODEL", existing.get("DEFAULT_ENG_MODEL", ""))
                )
            ),
            "DEFAULT_CODEX_MODEL": os.environ.get("DEFAULT_CODEX_MODEL", existing.get("DEFAULT_CODEX_MODEL", "")),
        }
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            body = stripped[7:] if stripped.startswith("export ") else stripped
            key = body.split("=", 1)[0] if "=" in body else ""
            if key in replacements:
                out.append('export %s="%s"' % (key, replacements[key]))
                seen.add(key)
            else:
                out.append(line)
        for key, value in replacements.items():
            if key not in seen:
                out.append('export %s="%s"' % (key, value))
        _write_env_file(CONFIG_PATH, out)
        return False
    host = os.environ.get("HOST_ID") or socket.gethostname().split(".")[0]
    kv = {
        "INSTALL_DIR": install,
        "HOST_ID": host,
        "QUEUE_SECRET": secrets.token_hex(24),  # FRESH per install; never baked
        "HUD_PORT": os.environ.get("HUD_PORT", "9900"),
        "TODO_PORT": os.environ.get("TODO_PORT", "9933"),
        "TTYD_PORT": os.environ.get("TTYD_PORT", "7681"),
        "TTYD_BROWSER_PORT": os.environ.get("TTYD_BROWSER_PORT", os.environ.get("TTYD_PORT", "7681")),
        # read-only ttyd behind the Terminal Graph's tiles (stock ttyd, no -W)
        "TTYD_RO_PORT": os.environ.get("TTYD_RO_PORT", str(int(os.environ.get("TTYD_PORT", "7681")) + 1)),
        "BIND_ADDR": os.environ.get("BIND_ADDR", "127.0.0.1"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DEFAULT_BACKEND": backend,
        "DEFAULT_ENG_MODEL": os.environ.get("DEFAULT_ENG_MODEL", "claude-opus-5-5"),
        "DEFAULT_CLAUDE_MODEL": os.environ.get(
            "DEFAULT_CLAUDE_MODEL", os.environ.get("DEFAULT_ENG_MODEL", "claude-opus-5-5")
        ),
        "DEFAULT_CODEX_MODEL": os.environ.get("DEFAULT_CODEX_MODEL", ""),
        "DEFAULT_GROK_MODEL": os.environ.get("DEFAULT_GROK_MODEL", ""),
        "QUEUE_DEAD_AFTER": "45",
        "HEARTBEAT_INTERVAL": "10",
    }
    kv["QUEUE_URL"] = os.environ.get("QUEUE_URL", "http://127.0.0.1:%s" % kv["HUD_PORT"])
    lines = ["# MyPlow runtime config — generated on first run (secrets here; never commit)"]
    lines += ['export %s="%s"' % (k, v) for k, v in kv.items()]
    _write_env_file(CONFIG_PATH, lines)
    return True
