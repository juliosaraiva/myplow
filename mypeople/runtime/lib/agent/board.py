def do_board(argv):
    """mp board [--compact] — print the whole board as JSON (no curl/secret/jq needed)."""
    board, err = _fetch_board()
    if err:
        print("ERROR: " + err, file=sys.stderr)
        return 1
    if "--compact" in argv:
        print(json.dumps(board, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(board, ensure_ascii=False, indent=2))
    return 0


def do_card(argv):
    """mp card <task_id> [--compact] — print a single card (exact id or unique prefix)."""
    ids = [a for a in argv if not a.startswith("--")]
    if not ids:
        print(USAGE["card"], file=sys.stderr)
        return 2
    tid = ids[0]
    board, err = _fetch_board()
    if err:
        print("ERROR: " + err, file=sys.stderr)
        return 1
    tasks = board.get("tasks", {}) or {}
    t = tasks.get(tid)
    if t is None:
        matches = [k for k in tasks if k.startswith(tid)]
        if len(matches) == 1:
            t = tasks[matches[0]]
        elif len(matches) > 1:
            print("ERROR: %r is ambiguous (%d cards match)" % (tid, len(matches)), file=sys.stderr)
            return 1
    if t is None:
        print("ERROR: no card matching %r" % tid, file=sys.stderr)
        return 1
    if "--compact" in argv:
        print(json.dumps(t, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(t, ensure_ascii=False, indent=2))
    return 0


def do_comment(argv):
    """mp comment <task_id> <body...> [--by <id>] — post a card comment as $AGENT_ID.

    Body comes from the remaining args, or from stdin when no body args are given."""
    if not argv:
        print(USAGE["comment"], file=sys.stderr)
        return 2
    tid = argv[0]
    rest = argv[1:]
    by = os.environ.get("AGENT_ID", "").strip()
    if "--by" in rest:
        i = rest.index("--by")
        by = rest[i + 1] if i + 1 < len(rest) else by
        rest = rest[:i] + rest[i + 2 :]
    by = full_aid(by) if by else "CEO"
    # --by is the ONLY flag and it was just consumed. Anything flag-shaped left over is a
    # command someone expected mp to parse, and it used to be POSTED as the comment instead
    # (card e949527956): a bare "-" went up as a one-character comment, and `--stdin` with a
    # heredoc posted the literal string "--stdin" while the real deliverable stayed on unread
    # stdin. There is no --stdin and no --file. A genuine body is ONE quoted argument, so it
    # is never a bare flag-shaped token; "- item one" has whitespace and passes.
    stray = [a for a in rest if a.startswith("-") and not any(c.isspace() for c in a)]
    if stray:
        print(
            "ERROR: mp comment takes the body as ONE quoted argument, or on stdin with no "
            "body arguments — there is no --stdin and no --file flag, and a flag-looking "
            "token is POSTED as the comment.\n"
            "  got:     mp comment %s %s\n"
            '  correct: mp comment %s "your comment text"\n'
            "           mp comment %s < body.md     (or a heredoc, with no body arguments)\n"
            "           add --by <agent_id> to attribute it\n"
            "  if the body really begins with %s, quote the whole body as one argument."
            % (tid, " ".join(rest), tid, tid, stray[0]),
            file=sys.stderr,
        )
        return 2
    body = " ".join(rest).strip()
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read().strip()
    if not body:
        print("ERROR: empty comment body (pass as args or pipe via stdin)", file=sys.stderr)
        return 2
    code, res = C.http_json(
        "POST", TODO_URL + "/todo/comment", {"task_id": tid, "by": by, "body": body}, HDR, timeout=12
    )
    if code != 200 or not (isinstance(res, dict) and res.get("ok")):
        print("ERROR: comment failed (HTTP %s): %s" % (code, str(res)[:200]), file=sys.stderr)
        return 1
    print("commented on %s as %s (id %s)" % (tid, by, res.get("id")))
    return 0


def do_owner(argv):
    """mp owner <assign|replace|reopen> <task_id> <agent_id> — set a card's owner.

    `--owner-task` only records ownership in the ROSTER; the card's assignee is settable solely
    through /todo/owner. Without this wrapper the Boss had no command for the second half and read
    mp's own source to find the endpoint -- engineer work its doctrine forbids. The server still
    owns every eligibility rule; this only carries the request.
    """
    if len(argv) < 3:
        print(USAGE["owner"], file=sys.stderr)
        return 2
    action, tid, agent_id = argv[0], argv[1], full_aid(argv[2])
    by = os.environ.get("AGENT_ID", "").strip()
    code, res = C.http_json(
        "POST",
        TODO_URL + "/todo/owner",
        {"action": action, "task_id": tid, "agent_id": agent_id, "by": full_aid(by) if by else by},
        HDR,
        timeout=12,
    )
    if code != 200 or not (isinstance(res, dict) and res.get("ok")):
        err = res.get("error") if isinstance(res, dict) else str(res)[:200]
        # ineligible_owner is the common one and its cause is never obvious from the word alone.
        hint = (
            "\nHINT: the agent must be alive, report to you, and have been spawned FOR this "
            "card (`mp spawn <id> --boss <you> --owner-task %s`)." % tid
            if err == "ineligible_owner"
            else ""
        )
        print("ERROR: owner %s failed (HTTP %s): %s%s" % (action, code, err, hint), file=sys.stderr)
        return 1
    print("owner of %s is now %s" % (tid, res.get("assignee", agent_id)))
    return 0


def _local_proof_path(target):
    """Return a local filesystem path if target names one, else None.

    A screenshot on disk is the common case, and it reaches us either as a plain path or as
    the file:// URL a browser/tool handed back. Both must be uploaded — a file:// URL stored
    as-is renders as a broken image on the board, because the page is served over http.
    """
    if target.startswith("file://"):
        return urllib.parse.unquote(urllib.parse.urlparse(target).path)
    if target.startswith(("http://", "https://", "/todo/")):
        return None
    if os.path.sep in target or target.startswith("~") or os.path.exists(target):
        return os.path.abspath(os.path.expanduser(target))
    return None


def do_proof(argv):
    """mp proof <task_id> <path-or-url> [body...] — attach durable proof.

    A local file is uploaded and served by the board; an http(s) link is stored as a link.
    """
    if len(argv) < 2:
        print(USAGE["proof"], file=sys.stderr)
        return 2
    tid, target = argv[0], argv[1]
    body = " ".join(argv[2:]).strip()
    local = _local_proof_path(target)
    if local is not None:
        if not os.path.isfile(local):
            print("ERROR: no such file: %s" % local, file=sys.stderr)
            print("       pass a readable file path, or an http(s) URL for a link proof", file=sys.stderr)
            return 2
        code, res = C.http_upload(TODO_URL + "/todo/proof", {"task_id": tid, "body": body}, local, HDR, timeout=120)
    else:
        code, res = C.http_json(
            "POST", TODO_URL + "/todo/proof", {"task_id": tid, "url": target, "body": body}, HDR, timeout=12
        )
    if code != 200 or not (isinstance(res, dict) and res.get("ok")):
        print("ERROR: proof failed (HTTP %s): %s" % (code, str(res)[:200]), file=sys.stderr)
        return 1
    print("proof attached to %s -> %s" % (tid, (res.get("proof") or {}).get("url", "")))
    return 0


def do_learning(argv):
    """Send typed data to the board with a separately provisioned scoped credential."""
    if argv:
        print(USAGE["learning"], file=sys.stderr)
        return 2
    import stat

    try:
        path = os.environ["MYPEOPLE_LEARNING_TOKEN_FILE"]
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="ascii") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError
            token = source.read(128).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError
        raw = sys.stdin.read(16_385)
        if len(raw) > 16_384:
            raise ValueError
        payload = json.loads(raw)
    except (KeyError, ValueError, OSError, RecursionError):
        print("ERROR: invalid learning input or credential file", file=sys.stderr)
        return 2
    code, result = C.http_json(
        "POST", TODO_URL + "/todo/learning", payload, {**HDR, "X-MyPlow-Learning": token}, timeout=12
    )
    if code != 200:
        print("ERROR: learning request failed (HTTP %s)" % code, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
