# ---------- atomic json io ----------
def atomic_write(path, data_bytes):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), int(time.time() * 1000))
    with open(tmp, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_JSON_SNAPSHOTS = {}


def read_json_tracked(path, default=None):
    """read_json that remembers what it read, so write_json_merged can tell which keys THIS
    process changed. Half of the roster transaction -- see write_json_merged."""
    data = read_json(path, default)
    _JSON_SNAPSHOTS[path] = copy.deepcopy(data)
    return data


def write_json_merged(path, obj):
    """Write a dict under an exclusive lock, merging onto whatever is on disk NOW.

    run/roster.json was read-modify-write with no mutual exclusion (card e949527956). Each
    write was atomic (atomic_write does fsync + os.replace, so no torn file), but twelve
    writers -- eleven save_roster call sites in bin/mp plus queue-client's heartbeat every
    10s -- meant last-writer-wins: an `mp kill` marking retired=True was erased by a
    heartbeat that had read the file 15ms earlier, and reconcile then correctly revived what
    looked like a crashed agent. eng-554 came back twice that way; eng-555's spawn entry was
    lost the same way and the fleet had no record of a running agent.

    Locking the write alone would fix nothing -- the stale read already happened. So the
    lock spans re-read + merge + write, and only the keys this process actually touched
    since its own read are applied. Keys another writer added meanwhile survive; keys this
    process deleted are deleted. Same flock pattern as _locked_notification_routes.

    A write CONSUMES the tracked read. A deletion is a key that was in this process's own
    read_json_tracked() and is missing from what it writes back -- one read, one write. Keeping
    the merged file as the next snapshot (as this used to) meant a caller that wrote a partial
    dict twice in one process deleted every key it had not mentioned: the Discord plugin's
    relaunch loop dropped 14 agents and their roles from roster.json that way (card
    f864c568e6). Without a fresh tracked read, a write only adds and updates.
    """
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = read_json(path, {}) or {}
        snapshot = _JSON_SNAPSHOTS.get(path)
        if not (isinstance(obj, dict) and isinstance(current, dict)):
            write_json(path, obj)
            _JSON_SNAPSHOTS.pop(path, None)
            return
        merged = dict(current)
        if isinstance(snapshot, dict):
            for k, v in obj.items():
                if k not in snapshot or snapshot[k] != v:
                    merged[k] = v
            for k in snapshot:
                if k not in obj:
                    merged.pop(k, None)
        else:
            # No tracked read since this process last wrote: apply everything, delete nothing.
            merged.update(obj)
        write_json(path, merged)
        _JSON_SNAPSHOTS.pop(path, None)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
