# ---------- model-switch hold ----------
# The model is a process argument. Switching it replaces the process and resumes the
# same session, and anything pasted into the old pane during that gap is gone. Every
# paste goes through send_when_ready, so while a switch is open a message is parked
# here instead of pasted. It is removed only after the paste reports success, and a
# later delivery sends what is left. A crash between a successful paste and the remove
# can deliver that one message twice; a failed paste never removes it.

import contextlib


def _hold_paths(aid):
    digest = hashlib.sha256(aid.encode("utf-8")).hexdigest()
    root = os.path.join(CFG["INSTALL_DIR"], "run", "model-hold")
    return os.path.join(root, digest + ".json"), os.path.join(root, digest + ".lock")


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def _load_hold(path):
    d = read_json(path, {}) or {}
    if not isinstance(d, dict):
        d = {}
    held = d.get("held") if isinstance(d.get("held"), list) else []
    inflight = d.get("inflight") if isinstance(d.get("inflight"), list) else []
    pids = []
    for pid in inflight:
        try:
            pids.append(int(pid))
        except (TypeError, ValueError):
            continue
    try:
        switch_pid = int(d.get("switch_pid") or 0)
    except (TypeError, ValueError):
        switch_pid = 0
    try:
        drainer = int(d.get("drainer") or 0)
    except (TypeError, ValueError):
        drainer = 0
    return {
        "switching": bool(d.get("switching")),
        "switch_pid": switch_pid,
        "drainer": drainer,
        "inflight": pids,
        "held": [m for m in held if isinstance(m, str)],
    }


def _save_hold(path, st):
    write_json(path, st)


def _reap_hold(st):
    """Drop pids this machine no longer has. A switcher that died must not park
    messages forever; what it already parked stays for the next delivery."""
    st["inflight"] = [pid for pid in st["inflight"] if _alive(pid)]
    if st["drainer"] and not _alive(st["drainer"]):
        st["drainer"] = 0
    if st["switching"] and not _alive(st["switch_pid"]):
        st["switching"] = False
        st["switch_pid"] = 0


@contextlib.contextmanager
def _locked_hold(aid):
    # Not re-entrant: the save happens on the way out, so a nested lock in this
    # same process would write a stale copy over the inner one.
    path, lock_path = _hold_paths(aid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = _load_hold(path)
        try:
            yield st
        finally:
            _save_hold(path, st)


def model_hold_agent(target):
    """mc-<sess>:<tab> -> this host's agent id, or "" for any other pane."""
    if not (isinstance(target, str) and target.startswith("mc-") and ":" in target):
        return ""
    sess, tab = target[3:].split(":", 1)
    if not sess or not tab:
        return ""
    return "%s/%s:%s" % (CFG["HOST_ID"], sess, tab)


def model_switch_busy(aid):
    """True while a live switcher holds the pane. A dead switcher's flag is cleared."""
    with _locked_hold(aid) as st:
        _reap_hold(st)
        return bool(st["switching"])


def model_hold_begin(aid, timeout):
    """Open the hold once nothing is mid-paste. False leaves it closed."""
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        with _locked_hold(aid) as st:
            _reap_hold(st)
            # A live switcher owns the flag even when nothing is mid-paste. Taking it
            # over here would close that switcher's hold out from under it.
            if st["switching"] and st["switch_pid"] != os.getpid():
                return False
            busy = bool(st["inflight"]) or (bool(st["drainer"]) and bool(st["held"]))
            if not busy:
                st["switching"] = True
                st["switch_pid"] = os.getpid()
                return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def model_hold_prepare(aid, message):
    """"held" if `message` was parked. "drain" if this caller owns a backlog that
    now includes `message` and must send the whole list (not the message again).
    "send" if this caller should paste `message` itself. "send" and "drain" count
    an in-flight delivery until model_hold_finish_delivery.
    """
    with _locked_hold(aid) as st:
        _reap_hold(st)
        if st["switching"]:
            st["held"].append(message)
            return "held"
        if st["held"]:
            st["held"].append(message)
            if st["drainer"]:
                return "held"
            st["drainer"] = os.getpid()
            st["inflight"].append(os.getpid())
            return "drain"
        st["inflight"].append(os.getpid())
        return "send"


def model_hold_finish_delivery(aid):
    with _locked_hold(aid) as st:
        _reap_hold(st)
        for i, pid in enumerate(st["inflight"]):
            if pid == os.getpid():
                del st["inflight"][i]
                break
        if st["drainer"] == os.getpid():
            st["drainer"] = 0


def model_hold_pending(aid):
    with _locked_hold(aid) as st:
        _reap_hold(st)
        return list(st["held"])


def release_model_hold(aid, send):
    """Paste each parked message with send(text)->bool, dropping one only after True.

    Closes the switch either way: this process is about to exit, and a flag left
    open would park every later message. Returns how many remain. send must paste
    directly -- calling send_when_ready from here would park the message again.
    """
    while True:
        with _locked_hold(aid) as st:
            _reap_hold(st)
            if not st["held"]:
                st["switching"] = False
                st["switch_pid"] = 0
                return 0
            msg = st["held"][0]
        if not send(msg):
            with _locked_hold(aid) as st:
                st["switching"] = False
                st["switch_pid"] = 0
                return len(st["held"])
        with _locked_hold(aid) as st:
            if st["held"] and st["held"][0] == msg:
                st["held"].pop(0)
