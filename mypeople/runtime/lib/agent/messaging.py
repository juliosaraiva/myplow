def do_send(argv):
    """mp send <agent_id> <msg...> — deliver a message into an agent's composer.

    The message is ARGV. Two guards below exist because the silent version of getting that
    wrong cost several agents most of a day (card 5676f76673): `mp send X --by Y <<EOF`
    delivered the literal string "--by Y", discarded the real body on stdin that nothing
    read, and printed "sent". The recipient got the same string every time, which is the
    tell — real truncation varies with the text, operator error repeats verbatim.
    """
    card = ""
    if argv[:1] == ["--card"]:
        card, argv = (argv[1] if len(argv) > 1 else ""), argv[2:]
        if not card or not os.environ.get("AGENT_ID", "").strip():
            print("ERROR: mp send --card needs a card id and $AGENT_ID: the thread shows who said it", file=sys.stderr)
            return 2
    if not argv:
        print(SEND_USAGE, file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    rest = argv[1:]
    # --card, first, is the only option, so any other flag standing alone as its own argv token is never part
    # of a message someone meant to send. Checked at EVERY position, not just the first:
    # the two habits in the wild were `mp send X --by Y <<EOF` (body discarded on unread
    # stdin) and `mp send X "body" --by Y` (body delivered with a literal " --by Y" glued
    # on) -- a leading-token check would have caught only the first and left the commoner
    # one silently corrupting. A real message that merely mentions --by is a single quoted
    # argument, so it never appears as a standalone token and is not caught.
    stray = [a for a in rest if a.startswith("--")]
    if stray:
        print(
            "ERROR: mp send takes the message as ARGUMENTS, not flags — there is no --by "
            "flag; reply_to comes from $AGENT_ID.\n"
            "  got:     mp send %s %s\n"
            '  correct: mp send %s "your message here"   (or pipe the body on stdin)\n'
            "  if the message really contains %s, quote the whole message as one argument."
            % (argv[0], " ".join(rest), argv[0], stray[0]),
            file=sys.stderr,
        )
        return 2
    msg = " ".join(rest).strip()
    # Gated on isatty exactly as do_comment is: an interactive `mp send <agent>` must print
    # usage, not block forever reading an empty terminal.
    if not msg and not sys.stdin.isatty():
        msg = sys.stdin.read().strip()
    if not msg:
        print(SEND_USAGE, file=sys.stderr)
        return 2
    # supervise.sh names the plugin it starts: the one place nearly every plugin's message passes through.
    msg += C.owner_rule(os.environ.get("MP_PLUGIN", ""))
    reply_to = os.environ.get("AGENT_ID", "").strip()
    if reply_to:
        reply_to = full_aid(reply_to)
    if not is_local(aid):
        payload = {"message": msg}
        if reply_to and reply_to != aid:
            payload["reply_to"] = reply_to
        if card:
            payload["card"] = card
        ok, result = remote_task("send", aid, payload, timeout=30)
        print(result)
        if ok and card:
            on_thread(card, reply_to, aid, msg)
        return 0 if ok else 1
    route_token = C.enqueue_notification_route(aid, reply_to, msg)
    # A Claude session still starting (or behind a dialog) is waited for briefly, then REFUSED
    # with rc 3: a paste there could vanish while this printed "sent". Retrying senders retry.
    why = C.send_when_ready(
        C.tmux_target(aid),
        msg + (C.card_note(card, reply_to) if card else ""),
        float(os.environ.get("MP_SEND_READY_WAIT", "10")),
    )
    ok = why in ("sent", "held")
    if not ok and route_token:
        C.cancel_notification_route(aid, route_token)
    print(why)
    if ok and card:
        on_thread(card, reply_to, aid, msg)
    return 0 if ok else (3 if why == "not_ready" else 1)


def on_thread(card, by, to, msg):
    """What mp send --card just delivered, on that card's thread. A card on another MyPlow is its board's to show."""
    code, res = C.http_json(
        "POST", TODO_URL + "/todo/comment", {"task_id": card, "by": by, "to": to, "body": msg}, HDR, timeout=12
    )
    if code != 200 or not (isinstance(res, dict) and res.get("ok")):
        print("card %s is not on this board: its thread is on the MyPlow that has it" % card)


def classify(pane, backend=None):
    low = pane.lower()
    if "do you want" in low or "❯ 1." in pane or "1. yes" in low or "would you like" in low:
        return "BLOCKED"
    tail = "\n".join(pane.strip().splitlines()[-6:]).lower()
    if backend == "grok":
        # grok's own vocabulary: the footer offers ctrl+c:cancel only while a turn is in flight
        if "run grok build in a project directory?" in low:
            return "BLOCKED"
        if "ctrl+c:cancel" in tail or "waiting for response" in tail or "esc to interrupt" in tail:
            return "WORKING"
        return "IDLE"
    if "esc to interrupt" in tail or "✳" in pane[-400:] or "running" in tail or "tokens" in tail:
        return "WORKING"
    return "IDLE"


def do_peek(argv):
    if not argv:
        print(USAGE["peek"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    if not is_local(aid):
        ok, result = remote_task("peek", aid, {}, timeout=30)
        print(result)
        return 0 if ok else 1
    pane = C.tmux_capture(C.tmux_target(aid))
    backend = (load_roster().get(aid) or {}).get("backend")
    print("[state:%s]" % classify(pane, backend))
    print(pane)
    return 0


def do_kill(argv):
    if not argv:
        print(USAGE["kill"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    reason = ""
    if "--reason" in argv:
        idx = argv.index("--reason")
        reason = " ".join(argv[idx + 1 :])
    if not is_local(aid):
        ok, result = remote_task("kill", aid, {"reason": reason}, timeout=60)
        print(result)
        return 0 if ok else 1
    _, sess, tab = C.parse_agent_id(aid)
    e = tmux_env()
    roster = load_roster()
    rr = roster.get(aid, {})
    recorder_stop = C.stop_recorder(sess, tab, rr.get("recorder"), HOST_ID)
    if aid in roster:
        roster[aid]["retired"] = True
        roster[aid]["retire_reason"] = reason or "killed"
        roster[aid]["retired_ts"] = time.time()
        roster[aid]["recorder_stop"] = recorder_stop
        save_roster(roster)
    C.http_json("POST", QUEUE_URL + "/agents/unregister", {"agent_id": aid}, HDR, timeout=6)
    if not close_agent_window(sess, tab, e):
        print("retired %s but its window is still open" % aid, file=sys.stderr)
        return 1
    print("killed %s" % aid)
    return 0


def do_answer(argv):
    if len(argv) < 2:
        print(USAGE["answer"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    n = argv[1]
    if not is_local(aid):
        ok, result = remote_task("answer", aid, {"n": n}, timeout=30)
        print(result)
        return 0 if ok else 1
    tgt = C.tmux_target(aid)
    C.pane_key(tgt, str(n))
    time.sleep(0.2)
    C.pane_key(tgt, "Enter")
    print("answered %s -> %s" % (aid, n))
    return 0


def session_files(backend, session_id):
    """Return persisted transcript files proving that a backend session can be resumed."""
    import glob

    if backend == "claude":
        pattern = os.path.expanduser("~/.claude/projects/**/%s.jsonl" % session_id)
    elif backend == "codex":
        pattern = os.path.expanduser("~/.codex/sessions/**/*%s.jsonl" % session_id)
    elif backend == "grok":
        root = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
        pattern = os.path.join(root, "sessions", "**", session_id, "chat_history.jsonl")
    else:
        return []
    return glob.glob(pattern, recursive=True)
