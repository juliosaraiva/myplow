def do_spawn(argv, resume_session=None, locked_role_ref=None, expected_role_digest=None):
    if not argv:
        print(USAGE["spawn"], file=sys.stderr)
        return 2
    aid = full_aid(argv[0])
    backend = None  # resolved after the flags: the Boss and new agents default differently
    cwd = None
    boss = None
    boss_given = False
    is_master = False
    model = None
    role = None
    recorded_role_ref = None  # revive pins the exact recorded name@semver
    owner_task_id = ""
    temporary = False
    record_flag = False
    community = False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--backend":
            i += 1
            backend = argv[i]
        elif a == "--cwd":
            i += 1
            cwd = argv[i]
        elif a == "--boss":
            boss_given = True
            i += 1
            boss = argv[i] if i < len(argv) else ""
        elif a == "--master":
            is_master = True
        elif a == "--model":
            i += 1
            model = argv[i]
        elif a == "--role":
            i += 1
            role = argv[i] if i < len(argv) else ""
        elif a == "--role-ref":
            i += 1
            recorded_role_ref = argv[i] if i < len(argv) else ""
        elif a == "--owner-task":
            i += 1
            owner_task_id = argv[i] if i < len(argv) else ""
        elif a == "--temporary":
            temporary = True
        elif a == "--record":
            record_flag = True
        elif a == "--community":
            community = True
        i += 1
    if temporary and owner_task_id:
        print("ERROR: --owner-task and --temporary are mutually exclusive", file=sys.stderr)
        return 2
    # revive/lock pins the exact recorded ref via kwarg OR --role-ref flag
    pilot_mode = herdr_enabled()
    if pilot_mode:
        try:
            herdr_target(aid)
            if role != "engineer" or not temporary or any((is_master, record_flag, community, resume_session, owner_task_id)):
                raise mpherdr.TransportError("Herdr pilot requires a local temporary engineer")
        except mpherdr.TransportError as error:
            return herdr_error(error)
    if locked_role_ref is None:
        locked_role_ref = recorded_role_ref
    # --- role/master coupling (§role spec) enforced BEFORE any tmux/roster ---
    role = (role or "").strip().lower() or None
    if role == "boss" and not is_master:
        print("ERROR: --role boss requires --master", file=sys.stderr)
        return 2
    if role == "engineer" and is_master:
        print("ERROR: --role engineer and --master cannot be combined", file=sys.stderr)
        return 2
    # NO BUNDLE-LESS BOSS, BY ANY PATH. --master IS what makes an agent a Boss, so the gate keys on
    # is_master -- NOT on the window being named "Boss". Keying on the name would let
    # `mp spawn <anything> --master` mint a second, bundle-less Boss with the doctrine missing.
    if is_master and role != "boss":
        print(
            "ERROR: %s is a Boss (--master) and must be spawned with --role boss; refusing a bundle-less Boss." % aid,
            file=sys.stderr,
        )
        return 2
    # empty --boss is a HARD ERROR (§5 spawn env-export contract)
    if boss_given and (boss is None or boss.strip() == ""):
        print(
            "ERROR: --boss given with an empty/whitespace value (unset $AGENT_ID bug); refusing to "
            "create an agent whose boss resolves to '<host>/'.",
            file=sys.stderr,
        )
        return 3
    if backend is None:
        backend = DEFAULT_BACKEND if is_master else DEFAULT_AGENT_BACKEND
    backend = (backend or "").strip().lower()
    if backend not in VALID_BACKENDS:
        print("ERROR: unsupported backend %r; choose claude, codex or grok" % backend, file=sys.stderr)
        return 2
    community_root = os.path.join(INSTALL_DIR, "run", "community")
    if community and (is_master or backend != "claude"):
        print(
            "ERROR: --community is for a claude engineer: only Claude's sandbox is proven to hold it", file=sys.stderr
        )
        return 2
    if community and cwd and not os.path.realpath(cwd).startswith(os.path.realpath(community_root) + os.sep):
        print(
            "ERROR: a --community engineer works in its own folder under %s, never in %s" % (community_root, cwd),
            file=sys.stderr,
        )
        return 2
    boss_id = full_aid(boss) if (boss_given and boss.strip()) else ""
    # queue.env as it is NOW beats this process's env: the Boss spawns with the env it was
    # launched with, so an edited default model reached new agents only after a Boss restart.
    # (This fix once lived only on a live install, 09-23, and the next sync erased it.) The Boss
    # gets it too: launched without --model it ran Claude Code's own default, Sonnet, for two weeks (#49).
    conf = C.read_config_file()
    default_model = {
        "claude": conf.get("DEFAULT_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
        "codex": conf.get("DEFAULT_CODEX_MODEL") or DEFAULT_CODEX_MODEL,
        "grok": conf.get("DEFAULT_GROK_MODEL") or DEFAULT_GROK_MODEL,
    }.get(backend) or None
    # Every Claude launch and revive names its effort, so none runs on whatever ~/.claude says.
    effort = (conf.get("DEFAULT_CLAUDE_EFFORT") or CFG["DEFAULT_CLAUDE_EFFORT"]) if backend == "claude" else ""
    # Only a model other than the default is a pin that revives replay. Recording the default as
    # one left 2 live agents and 308 retired records on claude-opus-5 after the default moved (10-29).
    model_pinned = bool(model) and model != default_model
    model = model or default_model
    if not is_local(aid) and community:
        print("ERROR: --community spawns only on this node", file=sys.stderr)
        return 2
    if not is_local(aid):
        ok, result = remote_task(
            "spawn",
            aid,
            {
                "backend": backend,
                "cwd": cwd or "",
                "boss": boss_id,
                "is_master": is_master,
                "model": model or "",
            },
        )
        print(result)
        return 0 if ok else 1
    why = C.backend_unready(backend)
    if why:
        print("ERROR: mp spawn: " + why, file=sys.stderr)
        return 2
    _, sess, tab = C.parse_agent_id(aid)
    if cwd is None:
        cwd = (
            os.path.join(community_root, tab)
            if community
            else os.path.join(INSTALL_DIR, "run", "boss" if is_master else "eng")
        )
    os.makedirs(cwd, exist_ok=True)
    if community:
        with open(os.path.join(cwd, "CLAUDE.md"), "w") as f:
            f.write(COMMUNITY_DOC)
    if backend == "claude":
        pretrust(cwd)
    elif backend == "grok":
        grok_pretrust()

    # --- FAIL-CLOSED ROLE PREFLIGHT + MOUNT (before any tmux pane / roster row) ---
    # Resolving and materializing here means a bad role leaves NO side effects: no window, no
    # roster entry, no half-born agent for the supervisor to find.
    role_bundle = None
    role_meta = {}
    if role:
        try:
            item = resolve_role(role, backend, locked_role_ref=locked_role_ref)
        except mprole.RoleError as ex:
            print("ERROR: role preflight failed (no tmux/roster created): %s" % ex, file=sys.stderr)
            return 2
        if expected_role_digest and item["digest"] != expected_role_digest:
            print(
                "ERROR: locked role drift for %s: expected digest %s, store now resolves %s. "
                "Refusing to revive under a different doctrine."
                % (aid, expected_role_digest[:12], item["digest"][:12]),
                file=sys.stderr,
            )
            return 5
        role_bundle = materialize_role(item, aid, backend)
        role_meta = {
            "role": role_bundle["role"],
            "role_ref": role_bundle["role_ref"],
            "role_digest": role_bundle["digest"],
            "personality_digest": role_bundle["personality_digest"],
            "skill_digests": {s["name"]: s["sha256"] for s in role_bundle["skills"]},
            "adapter_version": role_bundle["adapter_version"],
            "role_bundle": role_bundle["bundle_path"],
        }
        print(
            "role %s mounted: %s  digest=%s  bundle=%s"
            % (role, role_bundle["role_ref"], role_bundle["digest"][:12], role_bundle["bundle_path"])
        )

    launch = build_launch(
        aid,
        cwd,
        boss_id,
        is_master,
        model,
        backend,
        resume_session=resume_session,
        role_bundle=role_bundle,
        community=community,
        effort=effort,
    )
    if pilot_mode:
        return herdr_action("spawn", aid, cwd, launch)

    # audit spawn_cmd string (model pinned so /agents shows exact per-engineer model)
    parts = ["mp", "spawn", aid, "--backend", backend]
    if boss_id:
        parts += ["--boss", boss_id]
    if cwd:
        parts += ["--cwd", cwd]
    if is_master:
        parts += ["--master"]
    if model:
        parts += ["--model", model]
    if role:
        parts += ["--role", role]
    if owner_task_id:
        parts += ["--owner-task", owner_task_id]
    elif temporary:
        parts += ["--temporary"]
    if record_flag or recording_enabled_global():
        parts += ["--record"]
    if community:
        parts += ["--community"]
    spawn_cmd = " ".join(shlex.quote(x) for x in parts)

    e = tmux_env()
    reused = window_exists(sess, tab)
    if not reused:
        # window_exists says False for a tmux that refused the question too. Going on from that
        # starts a second copy of a live agent once tmux answers.
        windows = C.tmux_windows()
        if windows is None:
            print("ERROR: mp spawn: tmux is not answering; %s not spawned" % aid, file=sys.stderr)
            return 1
        reused = "mc-%s:%s" % (sess, tab) in windows
    if reused:
        # A standing window is not a running agent. Reusing one that holds anything else -- a bare
        # `claude` from the interactive auth step, a leftover shell -- is what shipped a Boss with
        # no bypass flag and made the Spawn buttons no-ops: the old code returned 0 here having
        # never run `launch`. Relaunch in place instead, and only ever stay hands-off when the
        # agent is genuinely there (True) or undeterminable (None).
        launched = window_runs_launched_agent(sess, tab, backend)
        tgt = "mc-%s:%s" % (sess, tab)
        if launched is False:
            print("mp spawn: %s exists but holds no correctly launched %s agent; relaunching" % (aid, backend))
            rw = C.tmux_run("respawn-window", "-k", "-t", tgt, "-c", cwd, launch, env=e, text=True)
            if rw.returncode != 0:
                print(
                    "ERROR: could not relaunch %s: %s"
                    % (aid, (rw.stderr or "").strip() or "tmux respawn-window failed"),
                    file=sys.stderr,
                )
                return 1
            subprocess.run(
                ["tmux", "set-option", "-t", tgt, "-w", "automatic-rename", "off"], env=e, capture_output=True
            )
            subprocess.run(["tmux", "set-option", "-t", tgt, "-w", "allow-rename", "off"], env=e, capture_output=True)
        elif launched is None:
            print("mp spawn: %s already exists and its pane could not be inspected; reusing" % aid)
        else:
            print("mp spawn: %s already running a launched %s agent, reusing" % (aid, backend))
    else:
        if not session_exists(sess):
            started = start_tmux_session(["-d", "-s", "mc-%s" % sess, "-n", tab, "-c", cwd, launch], e)
            if started.returncode != 0:
                print(
                    "ERROR: mp spawn: tmux refused a new session for %s: %s" % (aid, (started.stderr or "").strip()),
                    file=sys.stderr,
                )
                return 1
            C.tmux_run("set-option", "-g", "-t", "mc-%s" % sess, "automatic-rename", "off", env=e)
        else:
            # Checked before the roster row below: a refusal here used to leave an agent registered
            # alive with no window. tmux_run only retries a connection refused, so never two windows.
            made = C.tmux_run("new-window", "-d", "-t", "mc-%s" % sess, "-n", tab, "-c", cwd, launch, env=e, text=True)
            if made.returncode != 0:
                print(
                    "ERROR: mp spawn: tmux refused a window for %s: %s" % (aid, (made.stderr or "").strip()),
                    file=sys.stderr,
                )
                return 1
        tgt = "mc-%s:%s" % (sess, tab)
        # disable auto-rename so mc-<sess>:<tab> keeps resolving by name (§5 window naming)
        C.tmux_run("set-option", "-t", tgt, "-w", "automatic-rename", "off", env=e)
        C.tmux_run("set-option", "-t", tgt, "-w", "allow-rename", "off", env=e)
        C.tmux_run("rename-window", "-t", tgt, tab, env=e)
        # load the CEO tmux conf into this (possibly new) server
        conf = os.path.expanduser("~/.tmux.conf")
        if os.path.exists(conf):
            C.tmux_run("source-file", conf, env=e)

    # durable roster
    roster = load_roster()
    prior = roster.get(aid, {})
    # An agent born for a card is bound to it for life: /todo/owner refuses to hand a card to an
    # agent whose roster row does not say it was born for THAT card, so this is the durable half
    # of the ownership contract. A legacy row keeps whatever it already had.
    lifecycle = "owner" if owner_task_id else ("temporary" if temporary else prior.get("lifecycle", "legacy"))
    # A fresh spawn onto a DIFFERENT backend must not inherit the previous backend's session id.
    # It is not resumable by the new backend, and carrying it means the next accidental death sends
    # do_revive() hunting for a <new backend> transcript under a <old backend> id, finding nothing,
    # and refusing to restore an agent that should have recovered. A resume (resume_session set) is
    # the only case that legitimately carries a session id across, and reuse of a live window must
    # never touch the running agent's id.
    session_id = resume_session or prior.get("session_id", "")
    if session_id and not resume_session and not reused and prior.get("backend") not in ("", backend):
        print(
            "mp spawn: backend %s -> %s; dropping unusable %s session id %s"
            % (prior.get("backend"), backend, prior.get("backend"), session_id)
        )
        session_id = ""
    do_record = want_recorder(record_flag, prior)
    roster[aid] = {
        "agent_id": aid,
        "host": HOST_ID,
        "session": sess,
        "tab": tab,
        "backend": backend,
        "boss_id": boss_id,
        "cwd": cwd,
        "spawn_cmd": spawn_cmd,
        "model": (model or ""),
        "model_pinned": model_pinned,
        "effort": effort,
        "is_master": is_master,
        "retired": False,
        "lifecycle": lifecycle,
        "owner_task_id": owner_task_id,
        "trust": "community" if community else "",
        "session_id": session_id,
        "record": do_record,
        "summary": prior.get("summary", ""),
        # durable role/revive contract (§role spec). These are also what makes the
        # mounted personality VISIBLE: mp status and the board read them from here
        # rather than re-deriving anything.
        "role": role_meta.get("role", ""),
        "role_ref": role_meta.get("role_ref", ""),
        "role_digest": role_meta.get("role_digest", ""),
        "personality_digest": role_meta.get("personality_digest", ""),
        "skill_digests": role_meta.get("skill_digests", {}),
        "adapter_version": role_meta.get("adapter_version", ""),
        "role_bundle": role_meta.get("role_bundle", ""),
    }
    # preserve reap counters so a flapping host cannot infinite-loop respawns
    if prior.get("zombie_reaps") is not None:
        roster[aid]["zombie_reaps"] = prior.get("zombie_reaps")
    if not do_record:
        roster[aid].pop("recorder", None)
    save_roster(roster)

    # initial status; master gets a keyword-bearing, locked doctrine summary (J2c)
    # drop_session mirrors the roster: do_revive falls back to the status file, so a stale id left
    # here would resurrect the exact trap the roster just dropped.
    drop_session = session_id == "" and not resume_session and not reused
    if is_master:
        write_status(aid, "starting", backend, summary=DOCTRINE_SUMMARY, locked=True, drop_session=drop_session)
    else:
        write_status(aid, "starting", backend, drop_session=drop_session)

    # register with the queue-server
    C.http_json(
        "POST",
        QUEUE_URL + "/agents/register",
        {
            "agent_id": aid,
            "host": HOST_ID,
            "session": sess,
            "tab": tab,
            "backend": backend,
            "state": "alive",
            "boss_id": boss_id,
            "is_master": is_master,
            "summary": (DOCTRINE_SUMMARY if is_master else ""),
            "spawn_cmd": spawn_cmd,
            "model": (model or ""),
        },
        HDR,
        timeout=6,
    )

    if do_record:
        recorder = start_recorder(tab, sess, prior.get("recorder"))
        if recorder:
            roster = load_roster()
            if aid in roster and not roster[aid].get("retired"):
                roster[aid]["recorder"] = recorder
                roster[aid]["record"] = True
                save_roster(roster)
    else:
        # Default-off: record the intent so nothing re-attaches a read-only client to this pane.
        # Existing rec-* sessions are left alone here; `mp stop-recorders` is the fleet-wide relief.
        roster = load_roster()
        if aid in roster and not roster[aid].get("retired"):
            roster[aid]["record"] = False
            roster[aid].pop("recorder", None)
            save_roster(roster)

    # SELF-CHECK window naming
    if not window_exists(sess, tab):
        print("ERROR: window %s not named correctly after spawn" % tab, file=sys.stderr)
        return 4

    # Readiness gate: fresh windows must show a usable TUI (and pick up session_id if hooks wrote
    # it) before we claim success. Boss used to send into a blank pane -> eng-170 birth-zombie.
    # Note: Claude often withholds session_id until the first UserPromptSubmit; pane-ready is
    # enough for mp send to land. Reuse/resume already has an id.
    ready_sid = ""
    ready_how = ""
    if not reused and not resume_session:
        ready_sid, ready_how = wait_for_backend_ready(aid, timeout=SPAWN_READY_SEC)
        if ready_sid:
            roster = load_roster()
            if aid in roster and not roster[aid].get("retired"):
                roster[aid]["session_id"] = ready_sid
                save_roster(roster)
        elif not ready_how:
            print(
                "WARNING: %s window up but TUI/session not ready after %.0fs "
                "(birth-zombie risk)" % (aid, SPAWN_READY_SEC),
                file=sys.stderr,
            )

    if is_master and not resume_session:
        # send the onboarding prompt (doctrine internalized) after the TUI comes up
        doc = ""
        if os.path.exists(BOSS_DOC):
            doc = open(BOSS_DOC).read()
        prompt = (
            "You are the MyPlow Boss (agent %s). Read your doctrine below and internalize it, "
            "then wait for [todo] pings. Reply concisely that you are ready.\n\n%s" % (aid, doc[:6000])
        )
        C.send_when_ready("mc-%s:%s" % (sess, tab), prompt, 60)
    print("spawned %s" % aid)
    return 0


SEND_USAGE = (
    "usage: mp send [--card <task_id>] <agent_id> <msg...>   (message may come from stdin; "
    "--card also shows it in that card's thread)"
)
