"""Committed TODO completion source. Loaded into the board daemon's namespace."""

import base64
import binascii
import hmac
import hashlib
import stat

SUPERVISOR_JOURNAL = "supervisorJournal"
SUPERVISOR_WAKE = threading.Condition(LOCK)


def _supervisor_setting(name):
    return os.environ.get(name) or CFG.get(name, "")


def supervisor_enabled():
    return _supervisor_setting("SUPERVISOR_ENABLED") == "1" or bool(supervisor_journal_key())


def _supervisor_key(setting):
    path = _supervisor_setting(setting)
    if not path or CFG["BIND_ADDR"] != "127.0.0.1":
        return ""
    try:
        with open(path, encoding="ascii") as file:
            if stat.S_IMODE(os.fstat(file.fileno()).st_mode) & 0o077:
                return ""
            key = file.read(4096).strip()
            return key if len(key) >= 32 and key.isprintable() and not any(c.isspace() for c in key) else ""
    except (OSError, UnicodeError):
        return ""


def supervisor_journal_key():
    return _supervisor_key("SUPERVISOR_JOURNAL_KEY_FILE")


def supervisor_owner_authorized(handler):
    verifier = _supervisor_key("SUPERVISOR_OWNER_VERIFIER_FILE")
    journal = supervisor_journal_key()
    if not re.fullmatch(r"[0-9a-f]{64}", verifier) or not journal:
        return False
    value = handler.headers.get("Authorization", "")
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic" or len(encoded) > 8192:
        return False
    try:
        supplied = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return False
    owner, _, password = supplied.partition(b":")
    if owner != b"owner" or len(password) < 32 or not password.isascii():
        return False
    if password in (journal.encode(), CFG.get("QUEUE_SECRET", "").encode()):
        return False
    return hmac.compare_digest(verifier, hashlib.sha256(password).hexdigest())


def supervisor_owner_required(handler, ident):
    return supervisor_enabled() and (ident != "browser" or not supervisor_owner_authorized(handler))


def valid_supervisor_links(links):
    return (
        isinstance(links, list)
        and len(links) <= 50
        and all(
            isinstance(link, str)
            and len(link) <= 250
            and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,9}", link)
            for link in links
        )
    )


def supervisor_journal_authorized(handler, ident):
    key = supervisor_journal_key()
    value = handler.headers.get("X-MyPlow-Journal", "")
    return bool(key and ident == "machine" and value.isascii() and hmac.compare_digest(key, value))


def supervisor_events():
    with SUPERVISOR_WAKE:
        SUPERVISOR_WAKE.wait_for(lambda: bool(_ensure_snap().get(SUPERVISOR_JOURNAL)), timeout=2)
        return list(_ensure_snap().get(SUPERVISOR_JOURNAL, []))[:100]


def supervisor_ack(ids):
    if (
        not isinstance(ids, list)
        or len(ids) > 100
        or any(not isinstance(entry, str) or not re.fullmatch(r"[0-9a-f]{32}", entry) for entry in ids)
    ):
        return 400, {"error": "invalid_ack_ids"}
    with LOCK:
        board = board_for_edit()
        before = board.get(SUPERVISOR_JOURNAL, [])
        keys = set(ids)
        after = [row for row in before if row["id"] not in keys]
        if len(after) != len(before):
            board[SUPERVISOR_JOURNAL] = after
            if not save_board(board, changed=[]):
                return 409, {"error": "board_save_refused"}
    return 200, {"ok": True}


def commit_status_update(board, task, previous, ident, actor):
    """Caller holds LOCK. Journal and card commit together, before side effects."""
    state = task.get("state")
    action, message = None, None
    if state != previous:
        action = apply_owner_state_transition(task, previous, state, actor, defer=True)
        message = take_task_event(task, "state -> %s" % state)
    record = (
        state == "done"
        and previous != "done"
        and not task.get("test")
        and actor == "CEO"
        and ident == "browser"
        and supervisor_journal_key()
    )
    if record:
        board.setdefault(SUPERVISOR_JOURNAL, []).append(
            {"id": uuid.uuid4().hex, "card": task["id"], "at": now(), "verified": bool(task.get("verified"))}
        )
    if not save_board(board, changed=[task["id"]]):
        return False
    if record:
        SUPERVISOR_WAKE.notify_all()
    if action:
        request_owner_lifecycle(task, *action)
    if message:
        ping_boss(message)
    return True
