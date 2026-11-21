# ---------------- board store ----------------
BOARD_BACKEND = BS.select_backend(BOARD_PATH, C.CFG.get("BOARD_BACKEND"))


def _sqlite_path():
    return BS.db_path_for(BOARD_PATH)


def default_board():
    return {"version": 2, "order": [], "pinSeq": 0, "tasks": {}}


def board_exists():
    """Is there already a board in the ACTIVE backend?

    Must be asked of the backend, never by statting BOARD_PATH: under sqlite the JSON file never
    exists, so a plain os.path.exists() call reads every boot as a fresh install and seeds an empty
    board OVER the live one. The shrink guard hid this for busy boards (>5 cards) and let it through
    for small ones -- i.e. it deleted the board of every NEW user and nobody else's.
    """
    if BOARD_BACKEND == "sqlite":
        return BS.is_initialized(_sqlite_path())
    return os.path.exists(BOARD_PATH)


def seed_board_if_missing():
    """Boot step: give a genuinely fresh install an empty board, and never touch an existing one.
    Split out of main() so a test can run the real boot decision without starting a server."""
    if board_exists():
        return False
    save_board(default_board())
    return True


# Published board. Replaced as a whole after a commit; the object itself is never mutated.
# _SNAP_JSON is that object already encoded. It is filled when a reader asks, not on the
# write: encoding every commit made adds pay for the whole board, and eight readers each
# encoding it held the GIL so a writer could not run. One reader encodes, without holding
# LOCK; the others, and later polls, send the cached bytes.
_SNAP = None
_SNAP_JSON = None
# mtime of the sibling "<db>.reload" file. board-restore writes that file; our own saves
# do not, so a change means the snapshot is someone else's board and must be loaded again.
_SNAP_MARK = None
# Serializes the one reader that rebuilds _SNAP_JSON. Not LOCK, so a writer is not
# stuck behind that encode.
_ENCODE = threading.Lock()


def _install(snap):
    """Publish a new immutable board and drop the cached encoding of the previous one."""
    global _SNAP, _SNAP_JSON
    _SNAP_JSON = None
    _SNAP = snap


def _load_disk():
    if BOARD_BACKEND == "sqlite":
        return BS.load_board(_sqlite_path())
    b = C.read_json(BOARD_PATH, None)
    if not b or not isinstance(b, dict):
        return default_board()
    b.setdefault("order", [])
    b.setdefault("pinSeq", 0)
    b.setdefault("tasks", {})
    return b


def _board_disk_path():
    return _sqlite_path() if BOARD_BACKEND == "sqlite" else BOARD_PATH


def _reload_mark():
    try:
        st = os.stat(BS.reload_mark_path(_board_disk_path()))
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _ensure_snap():
    """Caller holds LOCK. Loads from disk on first use and after board-restore."""
    global _SNAP, _SNAP_JSON, _SNAP_MARK
    mark = _reload_mark()
    if _SNAP is None or mark != _SNAP_MARK:
        _SNAP = _load_disk()
        _SNAP_JSON = None
        _SNAP_MARK = mark
    return _SNAP


def board_snapshot():
    """The last committed board object. No lock on the hit path: the object is immutable."""
    if _SNAP is not None and _reload_mark() == _SNAP_MARK:
        return _SNAP
    with LOCK:
        return _ensure_snap()


def board_json():
    """The last committed board as JSON bytes. One encode per commit, shared by every reader.

    The encode does not hold LOCK, so a writer is not queued behind it. The bytes match
    JSONHandler._send's encoding of the same dict. A restore from another process drops
    the cache; the mark file is how that process tells us the database moved.
    """
    global _SNAP_JSON
    if _SNAP is not None and _SNAP_JSON is not None and _reload_mark() == _SNAP_MARK:
        return _SNAP_JSON
    with _ENCODE:
        if _SNAP is not None and _SNAP_JSON is not None and _reload_mark() == _SNAP_MARK:
            return _SNAP_JSON
        with LOCK:
            snap = _ensure_snap()
            cached = _SNAP_JSON
        if cached is not None:
            return cached
        encoded = json.dumps(snap).encode("utf-8")
        with LOCK:
            if _SNAP is snap and _SNAP_JSON is None:
                _SNAP_JSON = encoded
            elif _SNAP_JSON is not None:
                return _SNAP_JSON
        return encoded


def load_board():
    """A fresh dict. Unsaved mutations of the result never show up in the next load."""
    return copy.deepcopy(board_snapshot())


def board_for_edit():
    """Caller holds LOCK. Task objects are the snapshot's; copy one with task_for_edit before writing it."""
    snap = _ensure_snap()
    board = {"tasks": dict(snap.get("tasks") or {})}
    for k, v in snap.items():
        if k == "tasks":
            continue
        board[k] = copy.deepcopy(v) if isinstance(v, (list, dict)) else v
    board.setdefault("order", [])
    board.setdefault("pinSeq", 0)
    return board


def task_for_edit(board, tid):
    cur = (board.get("tasks") or {}).get(tid)
    if cur is None:
        return None
    task = copy.deepcopy(cur)
    board["tasks"][tid] = task
    return task


def _publish(board, changed, deleted):
    """Install the committed board. `changed is None` replaces the whole snapshot."""
    if changed is None:
        _install(copy.deepcopy(board))
        return
    tasks = dict((_SNAP or {}).get("tasks") or {})
    for tid in deleted or ():
        tasks.pop(tid, None)
    for tid in changed:
        task = (board.get("tasks") or {}).get(tid)
        if isinstance(task, dict):
            tasks[tid] = copy.deepcopy(task)
    snap = {}
    for k, v in board.items():
        if k == "tasks":
            continue
        snap[k] = copy.deepcopy(v) if isinstance(v, (list, dict)) else v
    snap["tasks"] = tasks
    _install(snap)


def save_board(board, changed=None, deleted=None):
    """Atomic save + catastrophic-shrink guard (§3).

    SQLite: pass `changed` (and `deleted`) to write only those cards. Omit them for a full
    diff, which is what a caller that mutated an arbitrary copy still needs. JSON installs
    still rewrite the file; the snapshot update is the same either way. Durable backups
    come from the decoupled board-exporter. Returns False when the shrink guard refuses,
    or when board-restore replaced the database after this snapshot was taken.
    """
    if _SNAP is not None and _reload_mark() != _SNAP_MARK:
        with LOCK:
            _ensure_snap()
        return False
    if BOARD_BACKEND == "sqlite":
        os.makedirs(TODOS_DIR, exist_ok=True)
        if changed is None:
            ok = BS.save_board(_sqlite_path(), board)
        else:
            ok = BS.save_changes(_sqlite_path(), board, changed, deleted or ())
    else:
        ok = _save_json(board)
    if ok:
        _publish(board, changed, deleted)
    return ok


def _save_json(board):
    os.makedirs(TODOS_DIR, exist_ok=True)
    ondisk = C.read_json(BOARD_PATH, None)
    new_n = len(board.get("tasks", {}))
    if ondisk and isinstance(ondisk, dict):
        old_n = len(ondisk.get("tasks", {}))
        if old_n > 5 and new_n < 0.5 * old_n:
            # refuse catastrophic shrink: write SUSPECT, keep the live board intact
            sp = "%s.SUSPECT.%d" % (BOARD_PATH, int(time.time()))
            C.atomic_write(sp, json.dumps(board, ensure_ascii=False).encode())
            sys.stderr.write("SUSPECT shrink refused: %d->%d, wrote %s\n" % (old_n, new_n, sp))
            return False
        # roll a backup of the current on-disk board before replacing
        try:
            bak = "%s.bak.%d" % (BOARD_PATH, int(time.time() * 1000))
            C.atomic_write(bak, json.dumps(ondisk, ensure_ascii=False).encode())
            baks = sorted(glob.glob("%s.bak.*" % BOARD_PATH))
            for old in baks[:-20]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass
    C.atomic_write(BOARD_PATH, json.dumps(board, ensure_ascii=False).encode())
    return True



def delete_comment(board, tid, cid, actor):
    """Remove ONE comment by id. Only its own author or the Boss may do it.

    No bulk, no wildcard: a delete is irreversible on every card of the board, so the scope is a
    single id the caller already knows, and an actor who is neither the author nor the Boss is
    refused. Returns (error, removed_comment)."""
    t = board["tasks"].get(tid)
    if not t:
        return "no_task", None
    for i, c in enumerate(t.get("comments") or []):
        if c.get("id") == cid:
            if not actor or actor not in (c.get("by"), BOSS_AGENT):
                return "not_author", None
            return None, t["comments"].pop(i)
    return "no_comment", None

def now():
    return time.time()


def _tmux_window_sizes():
    """Every window's size in ONE tmux call, keyed by the `mc-<sess>:<tab>` target.

    The graph polls this every 2s per open page, and it used to ask tmux once PER AGENT.
    That cost grows with the fleet while the answer is a single list: measured on a live
    27-agent install, the per-agent loop took 149ms and this one call takes 5ms.

    Failure stays soft, exactly as the per-agent version did: an empty map makes every node
    fall back to the standard 160x48 below, because a missing window size must never be the
    reason the fleet does not render.
    """
    try:
        out = subprocess.run(["tmux", "list-windows", "-a", "-F",
                              "#{session_name}:#{window_name} #{window_width} #{window_height}"],
                             capture_output=True, text=True, timeout=4,
                             env={**os.environ, "TMUX": ""})
        if out.returncode != 0:
            return {}
    except Exception:
        return {}
    sizes = {}
    for line in out.stdout.splitlines():
        parts = line.rsplit(" ", 2)
        if len(parts) != 3:
            continue
        try:
            sizes[parts[0]] = (int(parts[1]), int(parts[2]))
        except ValueError:
            continue
    return sizes


def _tmux_viewers():
    """Live viewer sessions per window name, or None if tmux could not say.

    A tile is a `_v_<tab>_<pid>_<random>` session (ttyd-attach.sh), so this is how the page learns
    a tile died: its frame is cross-origin and ttyd's client never reconnects. None is not zero:
    a tmux hiccup must not make the page reload every tile.
    """
    try:
        out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True, timeout=4,
                             env={**os.environ, "TMUX": ""})
        if out.returncode != 0:
            return None
    except Exception:
        return None
    viewers = {}
    for name in out.stdout.split():
        if name.startswith("_v_"):
            tab = name[3:].rsplit("_", 2)[0]
            viewers[tab] = viewers.get(tab, 0) + 1
    return viewers


