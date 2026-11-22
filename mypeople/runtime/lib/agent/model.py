"""mp model -- switch a live agent's model without retiring it or ending its session.

The CLI takes the model as a process argument (`claude --model … --resume <session>`,
and the same for Codex and Grok). Nothing in the runtime can change that argument
inside a running process, and typing `/model` into the composer is not a contract:
it differs by backend, it can land as a user turn, and the next crash resumes the
launch flag. So a live switch is a handoff in the same tmux window: respawn the
process on the new model, resume the same session id, and put the old process back
if that resume does not come up.

What carries over is whatever the backend reloads from that session (the transcript),
plus the roster: role, tools, cwd, backend, effort, Boss-ness, card ownership. The
team memory on disk is files, so it stays. What does not carry is the process itself:
an in-flight turn is cut, and a message that existed only inside the old process is
gone. We wait until the pane is idle before touching it, and messages that arrive
once the hold is open are parked and pasted, in order, after the new process is up.
"""

MODEL_SWITCH_GUARD = 180  # seconds ensure-boss / reconcile / the census leave this agent alone
INFLIGHT_WAIT = float(os.environ.get("MP_MODEL_INFLIGHT_WAIT") or 30)
IDLE_WAIT = float(os.environ.get("MP_MODEL_IDLE_WAIT") or 90)


def do_model(argv):
    if len(argv) != 2:
        print(USAGE["model"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    model = argv[1]
    if not _valid_model(model):
        print("ERROR: %r is not a model id" % model, file=sys.stderr)
        return 2
    if not is_local(aid):
        ok, result = remote_task("model", aid, {"model": model}, timeout=MODEL_SWITCH_GUARD + 30)
        print(result)
        return 0 if ok else 1
    return switch_live_model(aid, model)


def _valid_model(model):
    return bool(model) and not model.startswith("-") and not any(ch.isspace() for ch in model)


def default_model(backend):
    conf = C.read_config_file()
    return (
        {
            "claude": conf.get("DEFAULT_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
            "codex": conf.get("DEFAULT_CODEX_MODEL") or DEFAULT_CODEX_MODEL,
            "grok": conf.get("DEFAULT_GROK_MODEL") or DEFAULT_GROK_MODEL,
        }.get(backend)
        or ""
    )


def agent_is_idle(aid, backend):
    """True when the pane is at its composer and not mid-turn or in a dialog.

    A message the CLI has accepted but not written to the transcript lives only in
    that process. Idle is the closest signal that the transcript has caught up.
    """
    target = C.tmux_target(aid)
    if C.pane_input_state(target) != "ready":
        return False
    return classify(C.tmux_capture(target), backend) == "IDLE"


def wait_idle(aid, backend, timeout):
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        if agent_is_idle(aid, backend):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def session_diverged(aid, sid):
    """True when the status file names a different session than the one we resumed.

    An empty id is not a new session: Claude often withholds it until the first
    prompt. A different id means the resume failed open and started a fresh one.
    """
    got = (read_agent_status(aid).get("session_id") or "").strip()
    return bool(got) and got != sid


def _running(aid, backend, model, sid):
    """True/False/None: is this window the launched agent on `model`, resuming `sid`?

    None means the process tree could not be read. Callers must not replace a pane
    on that basis.
    """
    _, sess, tab = C.parse_agent_id(aid)
    tree = _pane_process_tree(sess, tab)
    if tree is None:
        return None
    if window_runs_launched_agent(sess, tab, backend) is not True:
        return False
    blob = "\n".join(tree)
    if sid not in blob:
        return False
    # An empty model is the launch with no --model. "" in blob is true for every string.
    if not model:
        return "--model" not in blob
    return "--model" in blob and model in blob


def process_has_resume(aid, backend, model, sid):
    return _running(aid, backend, model, sid) is True


def wait_resumed(aid, backend, model, sid, timeout):
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        if session_diverged(aid, sid):
            return False
        if process_has_resume(aid, backend, model, sid) and pane_looks_ready(aid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def respawn_agent(aid, cwd, launch):
    """Replace the process in the agent's existing window. The window stays, so a
    supervisor that keys off the window name does not see a death. False when tmux
    refused; the process may already have been killed (`respawn-window -k`)."""
    _, sess, tab = C.parse_agent_id(aid)
    if not window_exists(sess, tab):
        print("ERROR: %s has no window to switch" % aid, file=sys.stderr)
        return False
    e = tmux_env()
    tgt = "mc-%s:%s" % (sess, tab)
    rw = C.tmux_run("respawn-window", "-k", "-t", tgt, "-c", cwd or ".", launch, env=e, text=True)
    if rw.returncode != 0:
        print(
            "ERROR: could not relaunch %s: %s" % (aid, (rw.stderr or "").strip() or "tmux respawn-window failed"),
            file=sys.stderr,
        )
        return False
    C.tmux_run("set-option", "-t", tgt, "-w", "automatic-rename", "off", env=e)
    C.tmux_run("set-option", "-t", tgt, "-w", "allow-rename", "off", env=e)
    return True


def _session_id(aid, rr):
    sid = (rr.get("session_id") or "").strip()
    if sid:
        return sid
    return (read_agent_status(aid).get("session_id") or "").strip()


def _bundle_for(aid, rr, backend):
    """The same locked role the agent is running, or (None, error). A legacy agent
    has no role and comes back the same way."""
    role = _record_role(rr)
    if not role:
        return None, None
    try:
        item = resolve_role(role, backend, locked_role_ref=rr.get("role_ref") or None)
    except mprole.RoleError as ex:
        return None, "role preflight failed: %s" % ex
    expected = rr.get("role_digest") or ""
    if expected and item.get("digest") != expected:
        return None, (
            "locked role drift for %s: expected digest %s, store now resolves %s. "
            "Refusing to switch under a different doctrine." % (aid, expected[:12], item.get("digest", "")[:12])
        )
    return materialize_role(item, aid, backend), None


def _effort_for(rr, backend):
    effort = rr.get("effort") or ""
    if effort or backend != "claude":
        return effort
    conf = C.read_config_file()
    return conf.get("DEFAULT_CLAUDE_EFFORT") or CFG["DEFAULT_CLAUDE_EFFORT"]


def _launch_for(aid, rr, backend, model, sid, bundle):
    return build_launch(
        aid,
        rr.get("cwd") or "",
        rr.get("boss_id") or "",
        bool(rr.get("is_master")),
        model,
        backend,
        resume_session=sid,
        role_bundle=bundle,
        community=rr.get("trust") == "community",
        effort=_effort_for(rr, backend),
    )


def _rewrite_spawn_cmd(cmd, model):
    if not cmd:
        return cmd
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return cmd
    if "--model" in parts:
        i = parts.index("--model")
        if i + 1 < len(parts):
            parts[i + 1] = model
        else:
            parts.append(model)
    else:
        parts += ["--model", model]
    return " ".join(shlex.quote(p) for p in parts)


def _mark_switch(aid):
    roster = load_roster()
    rr = roster.get(aid)
    if not rr:
        return False
    rr["model_switch_until"] = time.time() + MODEL_SWITCH_GUARD
    roster[aid] = rr
    save_roster(roster)
    return True


def _clear_switch_guard(aid):
    roster = load_roster()
    rr = roster.get(aid)
    if not rr or not rr.get("model_switch_until"):
        return
    rr["model_switch_until"] = 0
    roster[aid] = rr
    save_roster(roster)


def _commit_model(aid, model, backend):
    roster = load_roster()
    rr = roster.get(aid)
    if not rr:
        return False
    rr["model"] = model
    rr["model_pinned"] = bool(model) and model != default_model(backend)
    rr["spawn_cmd"] = _rewrite_spawn_cmd(rr.get("spawn_cmd") or "", model)
    rr["model_switch_until"] = 0
    roster[aid] = rr
    save_roster(roster)
    C.http_json(
        "POST",
        QUEUE_URL + "/agents/register",
        {
            "agent_id": aid,
            "host": HOST_ID,
            "session": rr.get("session") or C.parse_agent_id(aid)[1],
            "tab": rr.get("tab") or C.parse_agent_id(aid)[2],
            "backend": backend,
            "state": "alive",
            "boss_id": rr.get("boss_id", ""),
            "is_master": rr.get("is_master", False),
            "summary": rr.get("summary", ""),
            "spawn_cmd": rr.get("spawn_cmd", ""),
            "model": model,
        },
        HDR,
        timeout=6,
    )
    return True


def _deliver_parked(aid):
    target = C.tmux_target(aid)
    return C.release_model_hold(aid, lambda text: C.tmux_send_message(target, text))


def _bring_up(aid, cwd, launch, backend, model, sid):
    """Respawn onto `launch` and wait until that process is the resumed session.
    A respawn that fails before killing anything reports success when the pane is
    already that process."""
    if not respawn_agent(aid, cwd, launch):
        return process_has_resume(aid, backend, model, sid)
    return wait_resumed(aid, backend, model, sid, SPAWN_READY_SEC)


def _restore(aid, cwd, old_launch, backend, old_model, sid):
    print("ERROR: %s did not resume on the new model; restoring %s" % (aid, old_model or "(default)"), file=sys.stderr)
    if old_model and process_has_resume(aid, backend, old_model, sid):
        print("still running on %s" % old_model, file=sys.stderr)
        return False
    if old_launch and _bring_up(aid, cwd, old_launch, backend, old_model, sid):
        print("restored %s on %s" % (aid, old_model or "(default)"), file=sys.stderr)
        return False
    print("ERROR: rollback failed; %s may be down. Parked messages are kept." % aid, file=sys.stderr)
    return False


def switch_live_model(aid, model):
    roster = load_roster()
    rr = roster.get(aid)
    if not rr:
        print("unknown agent %s" % aid, file=sys.stderr)
        return 1
    if rr.get("retired"):
        print(
            "ERROR: %s is retired; `mp revive %s --model %s` resumes it" % (aid, aid, model),
            file=sys.stderr,
        )
        return 1
    backend = rr.get("backend") or "claude"
    if backend not in VALID_BACKENDS:
        print("ERROR: unsupported backend %r" % backend, file=sys.stderr)
        return 1
    sid = _session_id(aid, rr)
    if not sid or not session_files(backend, sid):
        print(
            "ERROR: %s is not resumable: persisted %s session transcript is missing" % (aid, backend),
            file=sys.stderr,
        )
        return 1
    bundle, err = _bundle_for(aid, rr, backend)
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 1
    running = _running(aid, backend, model, sid)
    if running is None and (rr.get("model") or "") == model:
        print("could not inspect %s; leaving it on %s" % (aid, model))
        return 0
    if running is None:
        print("ERROR: could not inspect %s; not switching" % aid, file=sys.stderr)
        return 1
    if running:
        print("%s is already on %s" % (aid, model))
        left = _deliver_parked(aid)
        if left:
            print(
                "WARNING: %d message(s) for %s are parked and go out with the next mp send" % (left, aid),
                file=sys.stderr,
            )
        return 0
    if C.model_switch_busy(aid):
        print("ERROR: %s is already switching models" % aid, file=sys.stderr)
        return 1
    if not wait_idle(aid, backend, IDLE_WAIT):
        print(
            "ERROR: %s is mid-turn or not at its prompt; nothing changed. Retry when it is idle." % aid,
            file=sys.stderr,
        )
        return 1
    if not C.model_hold_begin(aid, INFLIGHT_WAIT):
        if C.model_switch_busy(aid):
            print("ERROR: %s is already switching models" % aid, file=sys.stderr)
        else:
            print("ERROR: %s still has a message in flight; nothing changed." % aid, file=sys.stderr)
        return 1
    old_model = rr.get("model") or ""
    cwd = rr.get("cwd") or ""
    try:
        if not _mark_switch(aid):
            print("unknown agent %s" % aid, file=sys.stderr)
            return 1
        new_launch = _launch_for(aid, rr, backend, model, sid, bundle)
        old_launch = _launch_for(aid, rr, backend, old_model, sid, bundle)
        if not _bring_up(aid, cwd, new_launch, backend, model, sid):
            _restore(aid, cwd, old_launch, backend, old_model, sid)
            return 1
        if not _commit_model(aid, model, backend):
            print("ERROR: %s resumed on %s but its roster row disappeared" % (aid, model), file=sys.stderr)
            _restore(aid, cwd, old_launch, backend, old_model, sid)
            return 1
        print("switched %s onto %s (session %s)" % (aid, model, sid))
        return 0
    finally:
        _clear_switch_guard(aid)
        left = _deliver_parked(aid)
        if left:
            print(
                "WARNING: %d message(s) for %s are parked and go out with the next mp send" % (left, aid),
                file=sys.stderr,
            )
