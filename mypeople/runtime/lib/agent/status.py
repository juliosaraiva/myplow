def do_status(argv):
    code, agents = C.http_json("GET", QUEUE_URL + "/agents", None, HDR, timeout=6)
    code2, clients = C.http_json("GET", QUEUE_URL + "/clients", None, HDR, timeout=6)
    roster = load_roster()
    drift = C.live_drift()
    if drift:
        print("!! live install is NOT VERSION %s: %d file(s) changed by hand since it was stamped: %s"
              " -- release through `make live`, never cp/patch into the install"
              % (C.version(), len(drift), ", ".join(drift[:6]) + (" ..." if len(drift) > 6 else "")))
    print("== agents ==")
    print("%-45s %-8s %-22s %s" % ("agent", "state", "role", "status"))
    # A Claude agent off the default model/effort is flagged (#49), by what its session last ran on.
    # With no reply to read: its launch flags, and what a launch left out Claude Code took from
    # ~/.claude/settings.json. A pin the owner chose is not flagged.
    conf, user = C.read_config_file(), C.read_json(os.path.expanduser("~/.claude/settings.json"), {}) or {}
    want = (conf.get("DEFAULT_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
            conf.get("DEFAULT_CLAUDE_EFFORT") or CFG["DEFAULT_CLAUDE_EFFORT"])
    for a in (agents or []):
        rr = roster.get(a["agent_id"], {})
        # Only this node's transcripts are here; another node's main:eng-1 is not ours.
        live = live_run(a["agent_id"], rr) if rr.get("backend") == "claude" and is_local(a["agent_id"]) \
            else (None, None)
        runs = (live[0] or rr.get("model") or user.get("model", "?"),
                live[1] or rr.get("effort") or user.get("effortLevel", "?"))
        off = rr.get("backend") == "claude" and not rr.get("model_pinned") and runs != want
        print("%-45s [%-6s] %-22s %s boss=%s%s" % (
            a["agent_id"], a.get("state", "?"), role_tag(a.get("agent_id", ""), a, roster),
            a.get("status", ""), a.get("boss_id", ""), "  !! runs %s/%s, not %s/%s" % (runs + want) if off else ""))
    print("== clients ==")
    for c in (clients or []):
        print("%-25s %s state=%s" % (c["hostname"], c.get("attach_base", ""), c.get("state", "")))
    return 0


def _fetch_board():
    """GET /todo/board with the machine credential. Returns (board_dict, err_str)."""
    code, board = C.http_json("GET", TODO_URL + "/todo/board", None, HDR, timeout=12)
    if code != 200 or not isinstance(board, dict):
        return None, "board fetch failed (HTTP %s): %s" % (code, str(board)[:200])
    return board, None


