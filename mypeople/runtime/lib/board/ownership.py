# ---------------- card ownership ----------------
def migrate_legacy_owner_fields(board):
    """Idempotently describe existing owners without fabricating their original time."""
    changed = False
    for task in board.get("tasks", {}).values():
        history = task.get("ownerHistory")
        if not isinstance(history, list):
            history = []
            task["ownerHistory"] = history
            changed = True
        owner = task.get("assignee", "")
        if owner and not history:
            history.append(
                {"action": "migrated_existing_owner", "agent_id": owner, "previous": "", "by": "system", "ts": now()}
            )
            changed = True
        if not isinstance(task.get("ownerNeedsReplacement"), bool):
            task["ownerNeedsReplacement"] = False
            changed = True
    return changed


def record_owner_event(task, action, agent_id, previous="", by=BOSS_AGENT):
    task.setdefault("ownerHistory", []).append(
        {"action": action, "agent_id": agent_id, "previous": previous, "by": by, "ts": now()}
    )


def queue_kill_owner(owner, reason):
    """Actually kill a card owner via the queue (does not depend on Boss being alive).

    Close/replace used to ONLY ping the Boss. If Boss was down, mid-crash, or a new session that
    missed the lifecycle message, owners became permanent ghosts (card 476fd89607 -- six eng still
    alive on closed cards). Submitting type=kill through queue-server is the same path /todo/boss
    and queue-client already use.
    """
    if not owner or not FULL_AGENT_ID.match(owner):
        return False
    try:
        code, r = C.http_json(
            "POST",
            CFG["QUEUE_URL"] + "/task/submit",
            {"type": "kill", "target_agent": owner, "payload": {"reason": reason}},
            {"X-Queue-Secret": SECRET},
            timeout=10,
        )
        return code == 200 and isinstance(r, dict) and bool(r.get("task_id"))
    except Exception:
        return False


def request_owner_lifecycle(task, action, owner=""):
    if task.get("test"):
        return
    tid = task["id"]
    if action == "close":
        # Primary: kill via queue so ghosts cannot outlive a dead/busy Boss.
        # Secondary: ping Boss for the audit trail / any extra bookkeeping.
        if owner:
            queue_kill_owner(owner, "card-%s-closed" % tid)
        ping_boss(
            "[todo lifecycle] Card %s was CLOSED by the CEO. Kill its owner %s now; "
            "preserve the assignee/history. Do not replace it unless the CEO reopens the card."
            % (tid, owner or "unassigned")
        )
    elif action == "reopen":
        ping_boss(
            "[todo lifecycle] Card %s was REOPENED by the CEO. CREATE one FRESH owner engineer "
            "with --owner-task %s; do not reuse %s." % (tid, tid, owner or "unassigned")
        )
    elif action == "replace":
        if owner:
            queue_kill_owner(owner, "card-%s-owner-replaced" % tid)
        ping_boss(
            "[todo lifecycle] Card %s owner was REPLACED. Kill prior owner %s; preserve history."
            % (tid, owner or "unassigned")
        )


def apply_owner_state_transition(task, previous, state, actor):
    was_terminal, is_terminal = previous in TERMINAL_STATES, state in TERMINAL_STATES
    owner = task.get("assignee", "")
    if not was_terminal and is_terminal:
        record_owner_event(task, "closed", owner, previous=owner, by=actor)
        task["ownerNeedsReplacement"] = False
        request_owner_lifecycle(task, "close", owner)
    elif was_terminal and not is_terminal:
        record_owner_event(task, "reopen_requested", "", previous=owner, by=actor)
        task["ownerNeedsReplacement"] = True
        request_owner_lifecycle(task, "reopen", owner)


def title_of(task):
    # Verbatim on purpose (CEO, card 5676f76673). This used to cut at 60 and the comment
    # body below at 120 -- silently, with no ellipsis -- so a Boss ping carried a reason it
    # never fully received: a 1932-char comment reached it as 120 chars, 93.8% dropped.
    # A notification that omits the ask is worse than a long one. Do not re-cap these.
    return task.get("text", "") or ""


def task_event_message(task, reason):
    return '[todo] task %s "%s": %s' % (task["id"], title_of(task), reason)


def take_task_event(task, reason):
    """Count a Boss ping on the card. None for a test card, which stays silent.

    The caller saves the card before ping_boss, so one fsync carries the counter and the
    card is durable before the Boss is told.
    """
    if task.get("test"):
        return None
    task["pingsToBoss"] = task.get("pingsToBoss", 0) + 1
    return task_event_message(task, reason)


def emit_task_event(task, reason):
    """Ping Boss for a non-test add / work-state change. Exempt {test} tasks."""
    msg = take_task_event(task, reason)
    if msg:
        ping_boss(msg)


def owner_relay(card_id, by, bodytext):
    """The owner's copy of a comment: their words, whole and unchanged, plus where it was said.

    Same shape the Boss relays by hand, so an agent reads exactly what it read before.
    """
    return '%s on card %s, verbatim: "%s"' % (by, card_id, bodytext)


def boss_chat_relay(card_id, by, bodytext):
    """The Boss chat card is a conversation, not a task: say so, and say where the answer goes."""
    return '[boss chat] %s: %s\n(reply on the Boss chat card: mp comment %s "your answer")' % (by, bodytext, card_id)


def ensure_boss_chat(board):
    """Caller holds LOCK. Give the board its one Boss chat card, pinned to the top by the UI.

    A flag, not a pin: pinRank is append-only, so a pin would land last among the pinned cards.
    Created silently -- no task event, no watchdog job -- since nobody is meant to own it.
    Returns True when the board changed."""
    t = board["tasks"].get(board.get("bossChat") or "")
    if t and t.get("bossChat"):
        return False
    tid = uuid.uuid4().hex[:10]
    board["tasks"][tid] = {
        "id": tid,
        "text": "Boss",
        "state": "working",
        "bossChat": True,
        "assignee": "",
        "ownerHistory": [],
        "ownerNeedsReplacement": False,
        "pinned": False,
        "pinRank": None,
        "doneCondition": "",
        "done": False,
        "verified": False,
        "unread": 0,
        "pingsToBoss": 0,
        "comments": [],
        "proofs": [],
        "test": False,
        "created": now(),
        "updated": now(),
    }
    board["order"].insert(0, tid)
    board["bossChat"] = tid
    return True


def deliver_comment(card_id, title, owner, by, bodytext, boss_chat=False):
    """Owner first, then the Boss. Runs off the request thread: mp_send blocks up to 25s."""
    if boss_chat:
        return ping_boss(boss_chat_relay(card_id, by, bodytext))
    delivered = False
    if owner:
        delivered = mp_send(owner, owner_relay(card_id, by, bodytext)) == 0
    # ping_boss stays the one seam for Boss notifications; it is what the verbatim-relay tests
    # pin, and what any future change to Boss delivery has to go through.
    ping_boss(
        '[todo] comment on %s "%s" by %s%s: %s'
        % (card_id, title, by, " (already delivered to %s)" % owner if delivered else "", bodytext)
    )


def take_comment_event(board, task, by, bodytext):
    """Count the comment ping and return deliver_comment's args, or None when nobody is told.

    `board` is unused; it stays in the signature because emit_comment_event's callers pass it.
    """
    if task.get("test"):
        return None
    if by == BOSS_AGENT:
        return None  # the Boss's own comment pings nobody
    task["pingsToBoss"] = task.get("pingsToBoss", 0) + 1
    # Straight to the card's owner. Routing through the Boss meant every word waited for a whole
    # agent turn before it moved: measured on the CEO's own card, a median of 18s between his
    # comment and the owner acting on it, worst case 439s. The board already knows the owner, so
    # that hop buys nothing. The Boss is still told -- and told the owner already has it, so it
    # does not relay the same words a second time.
    owner = (task.get("assignee") or "").strip()
    if owner in (by, BOSS_AGENT):
        owner = ""
    return (task["id"], title_of(task), owner, by, bodytext, bool(task.get("bossChat")))


def emit_comment_event(board, task, by, bodytext):
    args = take_comment_event(board, task, by, bodytext)
    if args:
        threading.Thread(target=deliver_comment, args=args, daemon=True).start()
