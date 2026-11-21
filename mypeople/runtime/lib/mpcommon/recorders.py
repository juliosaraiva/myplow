# ---------- terminal recorder lifecycle ----------
def recorder_identity(sess, tab, host=None):
    """Return the immutable identity used to validate a recorder before signalling it."""
    host = host or CFG["HOST_ID"]
    return {
        "session": "rec-%s" % tab,
        "target": "mc-%s:%s" % (sess, tab),
        "cast": os.path.expanduser("~/recordings/%s-%s.cast" % (host, tab)),
    }


def _ps(pid, field):
    try:
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", field + "="],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def _process_alive(pid):
    return bool(_ps(pid, "pid"))


def _recorder_pid_matches(pid, target, cast):
    """Reject PID reuse and unrelated asciinema jobs using the complete command identity."""
    if os.path.basename(_ps(pid, "comm")) != "asciinema":
        return False
    try:
        tokens = shlex.split(_ps(pid, "command"))
    except ValueError:
        return False
    if tokens:
        tokens[0] = os.path.basename(tokens[0])
    return tokens == ["asciinema", "rec", "--quiet", "--append", "-c", "TMUX=",
                      "tmux", "attach", "-rt", target, cast]


def _recorder_child_pids(pid):
    try:
        r = subprocess.run(["pgrep", "-P", str(int(pid))], capture_output=True, text=True,
                           timeout=2)
        return [int(x) for x in r.stdout.split() if x.isdigit()]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _recorder_child_matches(pid, parent_pid, target):
    if _ps(pid, "ppid").strip() != str(int(parent_pid)):
        return False
    try:
        tokens = shlex.split(_ps(pid, "command"))
    except ValueError:
        return False
    if tokens:
        tokens[0] = os.path.basename(tokens[0])
    return tokens == ["tmux", "attach", "-rt", target]


def current_recorder(sess, tab, host=None):
    """Describe the validated recorder reachable through the current tmux server."""
    ident = recorder_identity(sess, tab, host)
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + ident["session"],
             "-F", "#{pane_pid}|#{pid}|#{pane_dead}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        row = next((line for line in r.stdout.splitlines() if line), "")
        pane_pid, server_pid, dead = row.split("|", 2)
        pid = int(pane_pid)
        if dead != "0" or not _recorder_pid_matches(pid, ident["target"], ident["cast"]):
            return None
        return dict(ident, pid=pid, server_pid=int(server_pid), started_ts=time.time())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _wait_for_exit(pids, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and any(_process_alive(pid) for pid in pids):
        time.sleep(0.05)


def stop_recorder(sess, tab, metadata=None, host=None, include_current=True):
    """Gracefully stop only command-validated recorders for one agent.

    The durable PID is a fallback for a recorder stranded on an old tmux server/socket.  Every
    signal is guarded by target+cast command validation, so PID reuse or an unrelated live recorder
    cannot be touched.
    """
    ident = recorder_identity(sess, tab, host)
    candidates = []
    if include_current:
        current = current_recorder(sess, tab, host)
        if current:
            candidates.append(current["pid"])
    if isinstance(metadata, dict):
        try:
            pid = int(metadata.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if (pid and metadata.get("target") == ident["target"] and
                metadata.get("cast") == ident["cast"]):
            candidates.append(pid)
    candidates = list(dict.fromkeys(candidates))
    validated = [pid for pid in candidates
                 if _recorder_pid_matches(pid, ident["target"], ident["cast"])]
    child_terms = []
    for pid in validated:
        for child in _recorder_child_pids(pid):
            if _recorder_child_matches(child, pid, ident["target"]):
                try:
                    os.kill(child, signal.SIGTERM)
                    child_terms.append(child)
                except ProcessLookupError:
                    pass
    _wait_for_exit(validated)
    parent_terms = []
    for pid in validated:
        if (_process_alive(pid) and
                _recorder_pid_matches(pid, ident["target"], ident["cast"])):
            try:
                os.kill(pid, signal.SIGTERM)
                parent_terms.append(pid)
            except ProcessLookupError:
                pass
    _wait_for_exit(parent_terms)
    survivors = [pid for pid in validated
                 if _process_alive(pid) and
                 _recorder_pid_matches(pid, ident["target"], ident["cast"])]
    return {"requested_ts": time.time(), "validated": validated,
            "child_terms": child_terms, "parent_terms": parent_terms,
            "survivors": survivors}

def export_repo_path(cfg=None):
    """PER-INSTANCE git backup repo (§3 boardgit). Path carries an instance discriminator
    (TODO_PORT + sha1($INSTALL_DIR)[:8]) so two installs differing in INSTALL_DIR OR port never
    collide onto one repo. Overridable via EXPORT_REPO."""
    cfg = cfg or CFG
    override = os.environ.get("EXPORT_REPO") or cfg.get("EXPORT_REPO")
    if override:
        return override
    host = cfg.get("HOST_ID", "node")
    port = cfg.get("TODO_PORT", "9933")
    disc = hashlib.sha1(cfg.get("INSTALL_DIR", "").encode()).hexdigest()[:8]
    return os.path.expanduser("~/.mypeople/board-backup/%s-%s-%s" % (host, port, disc))

