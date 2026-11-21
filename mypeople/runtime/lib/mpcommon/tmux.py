# ---------- tmux message delivery (bracketed paste via buffer, with retry) ----------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def claude_composer_ready(screen):
    """True when a Claude Code screen shows its input box: a `❯` line directly under a
    horizontal rule, with the box's closing rule somewhere below it. Nothing else is a safe
    place to paste. A session still starting has no box
    yet, and text pasted then can vanish without trace; a dialog has none either, and the Enter
    that follows a paste would pick one of its options (the exit dialog's Enter is "review &
    send" for queued feedback drafts; an unanswered question's Enter picks its highlighted
    option). Menus draw `❯` too; a menu's last option can sit under a separator rule, but no
    rule closes it below, only "Enter to select".
    """
    lines = [l for l in (_ANSI.sub("", raw).rstrip() for raw in screen.splitlines()) if l.strip()][-25:]
    rule = [l.lstrip().startswith("\u2500" * 8) for l in lines]
    return any(
        lines[i].lstrip().startswith("\u276f") and rule[i - 1] and any(rule[i + 1 :]) for i in range(1, len(lines))
    )


def input_end(screen):
    """What the input box shows above the screen's last rule (back to the rule before it, or the top
    of the screen), whitespace removed; None with no rule on screen. Its end is the end of the input
    even when the box is taller than the window, where its `\u276f` line has scrolled away (11-08: the
    Boss's window took a 90x25 client's size)."""
    lines = [l for l in (_ANSI.sub("", raw).rstrip() for raw in screen.splitlines()) if l.strip()]
    rules = [i for i, l in enumerate(lines) if l.lstrip().startswith("\u2500" * 8)]
    if not rules:
        return None
    return re.sub(r"\s", "", "".join(lines[rules[-2] + 1 if len(rules) > 1 else 0 : rules[-1]]))


# Claude Code ends a paste that has had no bytes for 2 s (its input reader's IN_PASTE timeout) and
# takes the rest as typed keys, and a pty hands a paste over about 1 KB per read. So an agent busy
# enough to stall between two reads of one long paste got it in pieces, and sometimes lost the head
# (11-07: 83 of the Boss's 271 long messages). A piece this size arrives in one read, and one of at
# most 800 characters and 2 lines goes into the box as text, not a "[Pasted text]" chip.
PASTE_BYTES, PASTE_CHARS, PASTE_LINES = 900, 800, 2
PASTE_WAIT = 30  # seconds a long message waits, in all, for its pieces to show in the box
# What a caller gives one mp send: more than its longest delivery (MP_SEND_READY_WAIT 10 s for the
# box, PASTE_WAIT + 5 for another sender's turn, PASTE_WAIT for the pieces, then the Enter). A shorter
# one killed mp halfway through a long message, left it unsent in the box and pasted it again on the
# retry, and its sender was told it had failed (11-08, plow-chat at 30 s with the Boss stalled).
SEND_TIMEOUT = 90


def paste_pieces(message):
    """`message` in pieces of at most PASTE_BYTES UTF-8 bytes, PASTE_CHARS characters and
    PASTE_LINES line breaks, cut after a line break or a space when the piece has one."""
    pieces, rest = [], message
    while rest:
        used = breaks = 0
        for n, ch in enumerate(rest):
            used, breaks = used + len(ch.encode("utf-8")), breaks + (ch == "\n")
            if used > PASTE_BYTES or n >= PASTE_CHARS or breaks > PASTE_LINES:
                break
        else:
            return pieces + [rest]
        cut = rest.rfind("\n", 0, n) + 1 or rest.rfind(" ", 0, n) + 1 or n
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return pieces


def _target_backend(target):
    """The backend recorded for the agent behind mc-<sess>:<tab>, or None for any other pane."""
    if not (target.startswith("mc-") and ":" in target):
        return None
    sess, tab = target[3:].split(":", 1)
    st = read_json(os.path.join(CFG["INSTALL_DIR"], "status", "mc-%s" % sess, "%s.json" % tab), None)
    return (st or {}).get("backend") or None


def pane_input_state(target):
    """ "no_pane", "not_ready" (a Claude agent with no input box on screen: still starting, or a
    dialog is up), or "ready". A pane that is not a Claude agent (Codex, Grok, a test receiver)
    reads "ready": there is no composer shape to hold it to."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    if tmux_run("list-panes", "-t", target, env=env).returncode != 0:
        return "no_pane"
    if _target_backend(target) != "claude":
        return "ready"
    screen = tmux_run("capture-pane", "-e", "-p", "-t", target, env=env, text=True).stdout or ""
    return "ready" if claude_composer_ready(screen) else "not_ready"


def send_when_ready(target, message, wait):
    """tmux_send_message, after waiting up to `wait` seconds for a Claude target to reach its
    input box. Returns "sent", "not_ready", "no_pane", or for a retired agent a sentence saying
    so, so a caller that can retry later knows to, and one that cannot at least says why instead
    of printing "sent"."""
    deadline = time.time() + wait
    while pane_input_state(target) == "not_ready" and time.time() < deadline:
        time.sleep(1)
    if tmux_send_message(target, message):
        return "sent"
    state = pane_input_state(target)
    return "not_ready" if state == "not_ready" else (retired_note(target) or "no_pane")


def retired_note(target):
    """What a send to mc-<sess>:<tab> says when the roster has that agent retired, else "".

    A bare "no_pane" reads as a hiccup: on 2026-10-23 an A2A GO to an agent killed 4 h earlier
    got one, and its sender waited 1.5 h for a result (card 65c54015d5). Local mp send and the
    queue-client (which A2A deliveries go through) both return this to the sender."""
    sess, _, tab = target[3:].partition(":") if target.startswith("mc-") else ("", "", "")
    aid = "%s/%s:%s" % (CFG["HOST_ID"], sess, tab)
    rr = (read_json(os.path.join(CFG["INSTALL_DIR"], "run", "roster.json"), {}) or {}).get(aid) or {}
    if not rr.get("retired"):
        return ""
    mins = int((time.time() - rr.get("retired_ts", time.time())) // 60)
    return (
        "not sent: %s was retired %s ago (%s), so no one got this message; "
        "`mp revive %s` on its MyPlow brings it back"
        % (aid, "%d min" % mins if mins < 120 else "%d h" % (mins // 60), rr.get("retire_reason") or "mp kill", aid)
    )


# tmux serves on one thread with about 128 connections queued behind it. While it is stalled, a client
# past that queue is refused and says "no server running", rc 1, in 0.03 s, with the server alive
# (card a4607576c1). So a stall reads as no pane, no window and no session at once, and acting on that
# is what must never happen. Short enough that three tries still fit plow-chat's 30 s around mp send.
TMUX_STALL_WAIT = 8.0
TMUX_REFUSED = ("no server running", "error connecting")


def tmux_refused(r):
    """Did this tmux result fail at the connection, before any command ran?"""
    err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else str(r.stderr or "")
    return r.returncode != 0 and any(m in err for m in TMUX_REFUSED)


def tmux_server_gone(env=None):
    """True when no server can be behind the default socket, False when it may only be stalled.

    A server that takes the connection is there. A refusal on Linux means nothing listens: a full queue makes a client wait there
    instead. macOS refuses a full queue too, so there the server counts as there only if a tmux process holds this very socket. Any
    daemonized tmux used to count, and a builder's private `tmux -L` server held a crashed default one's recovery back (11-05).
    """
    if not hasattr(os, "getuid"):
        return True  # no uid, no tmux-<uid> directory, no ps (a Windows port on psmux): it cannot tell a stalled server from a gone one, so a refused command is final
    env = os.environ if env is None else env
    path = os.path.join(env.get("TMUX_TMPDIR") or "/tmp", "tmux-%d" % os.getuid(), "default")
    if not os.path.exists(path):
        return True
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        return False
    except OSError as e:
        if e.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK):
            return False  # a timeout, Linux's full queue, no permission: there is a server, maybe stalled
    finally:
        s.close()
    if sys.platform != "darwin":
        return True
    try:
        held = subprocess.run(
            ["lsof", "-U", "-a", "-c", "tmux", "-Fn"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not {path, os.path.realpath(path)} & {line[1:] for line in held.splitlines() if line.startswith("n")}


def tmux_run(*args, env=None, wait=None, **kw):
    """Run a tmux command, riding out a stalled server. Returns the last result."""
    if env is None:
        env = dict(os.environ)
        env.pop("TMUX", None)
    deadline = time.time() + (TMUX_STALL_WAIT if wait is None else wait)
    while True:
        r = subprocess.run(["tmux"] + list(args), env=env, capture_output=True, **kw)
        if not tmux_refused(r) or time.time() >= deadline or tmux_server_gone(env):
            return r
        time.sleep(0.25)


def tmux_windows(wait=None):
    """{"mc-main:eng-1", ...} from one tmux call, or None when the server cannot be read right now.

    None is not "no windows": deciding on it would retire and respawn live agents. Forking tmux per
    agent here and in mp reconcile, retired agents included, was 59% of the commands tmux logged.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
    # A client can block inside tmux on Linux before the retry loop sees a result.
    timeout = max(0.1, TMUX_STALL_WAIT if wait is None else wait)
    try:
        r = tmux_run(
            "list-windows", "-a", "-F", "#{session_name}:#{window_name}", env=env, wait=wait, timeout=timeout, text=True
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode == 0:
        return set(r.stdout.splitlines())
    return set() if tmux_server_gone(env) else None


def pane_key(target, key):
    """Press a key ("Enter", or a literal like "2") for the program in a pane. Returns True on success.

    send-keys goes to a pane's MODE first: with the window chooser open on an agent, a delivered
    message's Enter picked the highlighted row, jumped whoever was looking, and left the message
    unsubmitted in the composer; copy-mode ate it the same way. A paste always reaches the program.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)

    def tmux(*args, **kw):
        return tmux_run(*args, env=env, **kw)

    if tmux("display-message", "-p", "-t", target, "#{pane_in_mode}", text=True).stdout.strip() != "1":
        return tmux("send-keys", "-t", target, key).returncode == 0
    buf = "mp-" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
    raw = b"\r" if key == "Enter" else key.encode("utf-8")
    return (
        tmux("load-buffer", "-b", buf, "-", input=raw).returncode == 0
        and tmux("paste-buffer", "-r", "-d", "-b", buf, "-t", target).returncode == 0
    )


def tmux_send_message(target, message):
    """Deliver a message into a tmux pane's composer and submit it.
    target = mc-<sess>:<tab>. Returns True on success.

    Delivery is `load-buffer` + `paste-buffer -p`, never `send-keys -l`. send-keys was wrong
    in three ways at once (card 5676f76673):

      * It carries no bracketed-paste framing, so a composer reads every newline as Enter.
        One multi-paragraph message was submitted as N fragments; the ones landing while the
        agent was mid-turn were discarded, and it saw only the TAIL. Measured: one send ->
        three submissions. The TUIs' own burst heuristic hid this most of the time, which is
        why it surfaced as rare unexplained "lost during init" messages rather than a
        reproducible break -- correctness was a race.
      * It passes the message as a command ARGUMENT, and tmux refuses past a limit with
        "command too long". Bisected: 16000 chars delivered, 18000 chars dropped WHOLE.
      * Its result was never checked, so both failures returned True and printed "sent".

    load-buffer takes the message on stdin (no argument limit) and `paste-buffer -p` wraps it
    in ESC[200~ / ESC[201~ so the composer takes it as one atomic paste. Every tmux call is
    now checked, and the target is validated as a PANE -- `has-session` only checked the
    session name, so a wrong window reported success while every keystroke was discarded.

    To a Claude agent a long message goes as paste_pieces, each pasted only once the one before
    shows in the input box, so no paste spans two pty reads a stall can sit between. Deliveries
    to one pane take turns: another sender's Enter between two pieces submitted half a message
    (11-07, a [swarm chief] envelope reached the Boss as two).
    """
    if message is None or not str(message).strip():
        return False
    lock_path = os.path.join(CFG["INSTALL_DIR"], "run", "send-locks", re.sub(r"[^\w.-]", "_", target) + ".lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        give_up = time.time() + PASTE_WAIT + 5  # another sender's longest turn; a stuck one must not stop every other
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > give_up:
                    break
                time.sleep(0.05)
        return _send_message(target, str(message))


def _send_message(target, message):
    env = dict(os.environ)
    env.pop("TMUX", None)  # never target the caller's pane
    buf = "mp-" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")

    def tmux(*args, **kw):
        return tmux_run(*args, env=env, **kw)

    def box():
        return input_end(tmux("capture-pane", "-p", "-t", target, text=True).stdout or "")

    pieces = paste_pieces(message) if _target_backend(target) == "claude" else [message]
    pasted, deadline = 0, time.time() + PASTE_WAIT
    for attempt in range(3):
        while pasted < len(pieces):
            state = pane_input_state(target) if not pasted else "ready"
            if state == "not_ready":
                # Refuse rather than paste into a session that is starting or behind a dialog: the
                # sender learns it failed and can retry, instead of a "sent" that never arrived.
                return False
            before = box() if len(pieces) > 1 and state == "ready" else None
            # -p = bracketed paste markers, -d = drop the buffer once pasted.
            if (
                state == "no_pane"
                or tmux("load-buffer", "-b", buf, "-", input=pieces[pasted].encode("utf-8")).returncode
            ):
                break
            if tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target).returncode != 0:
                tmux("delete-buffer", "-b", buf)
                break
            pasted += 1  # a retry from here is for what is left: pasting a piece again would double it
            # Landed = the box ends with this piece, so the agent has read it and the pty is empty
            # again. A box that merely changed is not proof: the Boss's re-wraps on every resize of
            # its window, and pieces sent on that piled up in the pty and were split again (11-07).
            tail = re.sub(r"\s", "", pieces[pasted - 1])[-24:]
            while before is not None and time.time() < deadline:
                now = box()
                if now is not None and now != before and now.endswith(tail):
                    break
                time.sleep(0.05)
        if pasted < len(pieces):
            time.sleep(0.3)
            continue
        time.sleep(0.15)
        if not pane_key(target, "Enter"):
            time.sleep(0.3)
            continue
        # A composer that holds the paste as a chip needs a second Enter. Guarded by the
        # marker so we never submit an empty line into a pane that already accepted it.
        if "\n" in message:
            time.sleep(0.4)
            pane = tmux("capture-pane", "-p", "-t", target, "-S", "-30", text=True)
            if "[Pasted text" in (pane.stdout or ""):
                pane_key(target, "Enter")
        return True
    return False


def tmux_capture(target, lines=200):
    env = dict(os.environ)
    env.pop("TMUX", None)
    r = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", target, "-S", "-%d" % lines], env=env, capture_output=True, text=True
    )
    return r.stdout if r.returncode == 0 else ""
