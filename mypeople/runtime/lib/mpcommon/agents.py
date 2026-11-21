# ---------- agent_id helpers ----------
def parse_agent_id(aid):
    """<host>/<sess>:<tab> -> (host, sess, tab). Robust to missing pieces."""
    host = sess = tab = ""
    if "/" in aid:
        host, rest = aid.split("/", 1)
    else:
        rest = aid
    if ":" in rest:
        sess, tab = rest.split(":", 1)
    else:
        sess = rest
    return host, sess, tab


def _notification_route_paths(target_agent):
    digest = hashlib.sha256(target_agent.encode("utf-8")).hexdigest()
    root = os.path.join(CFG["INSTALL_DIR"], "run", "notification-routes")
    return os.path.join(root, digest + ".json"), os.path.join(root, digest + ".lock")


def _locked_notification_routes(target_agent, mutate):
    """Atomically update the per-agent FIFO used to route the next Stop notification."""
    path, lock_path = _notification_route_paths(target_agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        routes = read_json(path, []) or []
        if not isinstance(routes, list):
            routes = []
        result, routes = mutate(routes)
        if routes:
            write_json(path, routes)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return result


ROUTE_TTL = 24 * 3600  # a reply nobody claimed within a day was never coming


def _route_head(message):
    return " ".join((message or "").split())[:160]


_VARIABLE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render_template(template, values):
    """Fill a message's variables, in one pass, so words a sender wrote are never read as a variable. A name that is not a
    variable goes out as written."""
    return _VARIABLE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def saved_template(plugin):
    """The owner's edited message for a plugin, or "": then the plugin's own words go out, unchanged. Read per message."""
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "state", plugin, "template.txt")) as f:
            return f.read()
    except OSError:
        return ""


def plugin_text(plugin, template, values):
    """What a plugin hands an agent: its own words, or the owner's edit of them, with this message's values filled in."""
    return render_template(without_old_name(saved_template(plugin), template) or template, values)


# The variables a message cannot lose: the agent would get none of what was said, could not tell who sent it, or could not answer.
REQUIRED_VARIABLES = ("their_message", "sender", "user_id", "trust_level", "youtube_channel", "reply_command")
# Who sent it, filled in by the plugin for each message, so nobody can type them. The words around them are the owner's. They must stand
# before the person's words: if those came first, a sender could open with words that read like MyPlow's own and pose as the owner.
IDENTITY_VARIABLES = ("sender", "user_id", "trust_level", "youtube_channel")
# These only warn, in the page: the owner may mean to drop them.
WARNED_VARIABLES = ("safety_line", "owner_line", "trust_rules", "confirm_command")


def in_template(names, template):
    return [v for v in names if "{%s}" % v in template]


def checked_template(default, text):
    """A message as written, or ValueError. Every way of saving one comes through here, so none can lose a variable."""
    text = "".join(c if c == "\n" or c.isprintable() else " " for c in str(text or "")).strip()[:8000]
    if not text:
        raise ValueError("a message cannot be empty")
    lost = [v for v in in_template(REQUIRED_VARIABLES, default) if "{%s}" % v not in text]
    if lost:
        raise ValueError(
            "not saved: without %s the agent cannot be told who this is, what they said, or how to answer"
            % ", ".join("{%s}" % v for v in lost)
        )
    if "{their_message}" in text:
        late = [
            v
            for v in in_template(IDENTITY_VARIABLES, default)
            if text.index("{%s}" % v) > text.index("{their_message}")
        ]
        if late:
            raise ValueError(
                "not saved: {their_message} has to come after %s, or what someone sends could arrive first and read like "
                "MyPlow's own words" % ", ".join("{%s}" % v for v in late)
            )
    return text


def backend_unready(backend):
    """Why an agent on this backend cannot run on this machine, or "". Started anyway, its window runs nothing and the page says Down
    with no reason (a Codex Boss chosen on a Mac whose one-liner installed Claude only, 11-05). Claude's login is the node's login
    gate's job; Codex's is checked here, as it is the one a person picks on its own."""
    name = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}.get(backend, backend)
    if not shutil.which(backend):
        how = {
            "claude": "curl -fsSL https://claude.ai/install.sh | bash && claude auth login",
            "codex": "brew install --cask codex && codex login"
            if sys.platform == "darwin"
            else "npm install -g @openai/codex && codex login",
            "grok": "install the Grok CLI, then grok login",
        }.get(backend, "install it")
        return "%s is not installed on this machine. In a terminal, run: %s" % (name, how)
    if backend == "codex":
        try:
            if subprocess.run(["codex", "login", "status"], capture_output=True, timeout=15).returncode != 0:
                return "Codex is installed but not signed in. In a terminal, run: codex login"
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def without_old_name(saved, default):
    """Text saved on 5.22.167 says {trust_stamp}, its one name for the words that say who sent it, {who} for the sender on YouTube and
    {safety_line} for Plow Chat's line that says it is the owner. Read in today's words, so it delivers right and the page can show and
    check it. default: the same message's own words, which have those words before {their_message}."""
    words = default[: default.find("{their_message}")].rsplit("\n", 1)[-1].replace("{seen_line}", "").rstrip(' "')
    if (
        "{owner_line}" in default and "{safety_line}" not in default
    ):  # Plow Chat's, then; a message with both keeps both
        saved = saved.replace("{safety_line}", "{owner_line}")
    return (
        saved.replace("{who}", "{sender}")
        .replace("{trust_stamp}:", words.rstrip(":") + ":")
        .replace("{trust_stamp}", words)
    )


def _rule_text(plugin):
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "state", plugin or "-", "wrap.txt")) as f:
            return " ".join(f.read().split())
    except OSError:
        return ""


def owner_rule(plugin):
    """The owner's own text for what a plugin hands an agent, from the Plugins page, as a line to add to a message. Read per
    message, so an edit needs no restart. The command is why a sender typing the same words cannot pass as the owner."""
    text = _rule_text(plugin)
    return (
        (
            "\n(Your owner's rule for this plugin: %s Your MyPlow added this line, not the sender; confirm it by running: "
            "mp plugins wrap %s)" % (text, plugin)
        )
        if text
        else ""
    )


def cli_session(pane_pid, run):
    """What changes when the CLI in a pane's `while true; do cli; done` restarts: its pid. The pane id and the container's
    start time do not. run(shell_line) gives its stdout wherever the pane lives; /proc is for an image with no pgrep.
    "" when it cannot tell."""
    p = str(pane_pid or "").strip()
    return (
        " ".join(str(run("pgrep -n -P %s || cat /proc/%s/task/%s/children" % (p, p, p)) or "").split())
        if p.isdigit()
        else ""
    )


def give_rule(plugin, session, paste):
    """The owner's text for a plugin that pastes strangers' words into its own locked agent. That agent reads all of such an
    input as data, so a line added to it would never count: the rule goes as an input of its own, once per CLI session and
    per change. paste(text) is True only if it went in, and only then does it count as told; False here means it did not go
    in, so the caller holds the stranger's message back and tries again. A session it cannot tell ("") never counts as told:
    the rule repeats rather than go missing."""
    path, text = os.path.join(CFG["INSTALL_DIR"], "state", plugin, "wrap.given"), _rule_text(plugin)
    was_session, was_text = read_json(path, None) or ["", ""]
    if (not text and not was_text) or (session and (was_session, was_text) == (session, text)):
        return True
    if text:
        note = "Your owner's rule for this plugin, from their app, for every message from now on: %s" % text
    else:
        note = "Your owner removed their rule for this plugin." if was_session == session else ""
    if note and not paste(note):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, json.dumps([session, text]).encode("utf-8"))
    return True


def card_note(card, frm):
    """What mp send --card adds for the agent it reaches, so the answer lands on the same thread."""
    return '\n(On card %s. To answer on it: mp send --card %s %s "...")' % (card, card, frm)


def enqueue_notification_route(target_agent, reply_to, message=""):
    """Queue reply_to to hear how target_agent's turn on THIS message ends. Returns a token."""
    if not target_agent or not reply_to or target_agent == reply_to:
        return ""
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")

    def append(routes):
        routes.append({"token": token, "reply_to": reply_to, "ts": time.time(), "head": _route_head(message)})
        return token, routes

    return _locked_notification_routes(target_agent, append)


def cancel_notification_route(target_agent, token):
    """Remove a route when its corresponding message could not be delivered."""
    if not target_agent or not token:
        return False

    def cancel(routes):
        kept = [route for route in routes if route.get("token") != token]
        return len(kept) != len(routes), kept

    return _locked_notification_routes(target_agent, cancel)


def open_notification_routes(target_agent, text):
    """Mark the routes whose message reached this turn (it is in TEXT). Drops expired ones."""
    if not target_agent:
        return
    seen = " ".join((text or "").split())

    def mark(routes):
        now, kept = time.time(), []
        for r in routes:
            if not isinstance(r, dict) or now - r.get("ts", 0) > ROUTE_TTL:
                continue
            if r.get("head") and r["head"] in seen:
                r["opened"] = True
            kept.append(r)
        return None, kept

    _locked_notification_routes(target_agent, mark)


def claim_notification_routes(target_agent):
    """Who asked to hear how this turn ended: the senders whose message reached it.

    This used to pop the OLDEST route on every Stop, whatever the turn was about, so a turn
    nobody asked for (the Boss talking to the owner) reached whoever messaged it weeks ago
    (card 8f490e73e5). A route queued before pairing (no "head") is still honoured oldest-first,
    one per turn, until it expires. [] when nobody asked."""
    if not target_agent:
        return []

    def claim(routes):
        now = time.time()
        live = [r for r in routes if isinstance(r, dict) and r.get("reply_to") and now - r.get("ts", 0) <= ROUTE_TTL]
        mine = [r for r in live if r.get("opened")] or [r for r in live if "head" not in r][:1]
        return list(dict.fromkeys(r["reply_to"] for r in mine)), [r for r in live if r not in mine]

    return _locked_notification_routes(target_agent, claim)


def tmux_target(aid):
    _, sess, tab = parse_agent_id(aid)
    return "mc-%s:%s" % (sess, tab)
