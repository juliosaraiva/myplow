def do_ensure_boss(argv):
    """Keep the singleton Boss alive without ever replacing a resumable session.

    A genuinely new node has no roster entry and may spawn its first Boss. Once an entry exists,
    recovery is strict: mark a missing window retired and route through do_revive(), which restores
    the persisted backend session and original structured spawn flags or surfaces an error.

    A DELIBERATELY killed Boss stays dead (card 0cc0bde980). `mp kill` records durable intent in
    roster.json; this honours it with exactly the gate do_reconcile applies to every other agent.
    Only a death the system itself observed (ACCIDENTAL_REASONS) is auto-recovered; a deliberate
    stop is restored only by an on-purpose `mp spawn`, which clears the intent itself.
    """
    aid_arg = next((x for x in argv if not x.startswith("--") and x not in VALID_BACKENDS),
                   "%s/main:Boss" % HOST_ID)
    backend = DEFAULT_BACKEND
    if "--backend" in argv:
        idx = argv.index("--backend")
        backend = argv[idx + 1] if idx + 1 < len(argv) else ""
    backend = (backend or "").strip().lower()
    if backend not in VALID_BACKENDS:
        print("ERROR: unsupported backend %r; choose claude, codex or grok" % backend, file=sys.stderr)
        return 2
    aid = full_aid(aid_arg)
    _, sess, tab = C.parse_agent_id(aid)
    if C.tmux_windows() is None:
        print("tmux is not answering; leaving %s alone" % aid)
        return 0
    # "Window is there" was the whole liveness test, so a Boss window holding a bare `claude` from
    # the interactive auth step was adopted as-is and never relaunched with its bypass flag. Only a
    # window that actually runs a launched agent counts as alive; an undeterminable pane is left
    # alone rather than churned.
    if window_exists(sess, tab) and window_runs_launched_agent(sess, tab, backend) is not False:
        print("Boss already alive: %s" % aid)
        return 0
    roster = load_roster()
    rr = roster.get(aid)
    # Intent gate FIRST: a kill tombstone carries the intent but not is_master, and must stay dead
    # quietly rather than fall through to the not-a-Boss error the supervisor would log every 15s.
    if rr is not None and rr.get("retired"):
        reason = rr.get("retire_reason", "")
        if reason not in ACCIDENTAL_REASONS:
            print("%s was deliberately retired (%s); not respawning" % (aid, reason))
            return 0
    if rr is None:
        # A new node's FIRST Boss is born mounted -- this is the path the supervisor takes, so it
        # is the one that must never yield a bundle-less Boss.
        return do_spawn([aid, "--master", "--role", "boss", "--backend", backend])
    if not rr.get("is_master"):
        print("ERROR: %s exists but is not recorded as the Boss" % aid, file=sys.stderr)
        return 1
    if not rr.get("retired"):
        rr["retired"] = True
        rr["retire_reason"] = "boss-window-missing"
        rr["retired_ts"] = time.time()
        save_roster(roster)
    return do_revive([aid])


def _record_role(rr):
    """The role a restored agent must be reborn with.

    A legacy record has no role. A legacy Boss is UPGRADED to the mounted boss role rather than
    restored bundle-less (a bundle-less Boss is the bug this closes, and refusing to revive the
    Boss at all would be worse). A legacy engineer stays legacy -- it renders as "-" in mp status,
    so an unmounted agent stays visible rather than being silently rewritten.
    """
    return rr.get("role") or ("boss" if rr.get("is_master") else "")


def spawn_args_from_record(aid, rr):
    """Rebuild the structured spawn arguments from a durable roster record."""
    args = [aid, "--backend", rr.get("backend", "claude")]
    if rr.get("boss_id"):
        args += ["--boss", rr["boss_id"]]
    if rr.get("cwd"):
        args += ["--cwd", rr["cwd"]]
    if rr.get("is_master"):
        args += ["--master"]
    role = _record_role(rr)
    if role:
        args += ["--role", role]
    # A recovered owner must come back still bound to its card, or /todo/owner will refuse to
    # recognise it and the card is left ownerless with a live agent sitting on it.
    if rr.get("owner_task_id"):
        args += ["--owner-task", rr["owner_task_id"]]
    elif rr.get("lifecycle") == "temporary":
        args += ["--temporary"]
    if rr.get("trust") == "community":
        args += ["--community"]
    if rr.get("model_pinned") and rr.get("model"):
        args += ["--model", rr["model"]]
    return args


def reap_birth_zombie(aid, rr):
    """Kill a birth-zombie window and respawn the same roster identity (fresh backend session).

    Returns (ok: bool, detail: str). Capped per agent by ZOMBIE_REAP_MAX so a host that simply
    cannot boot the backend loops a bounded number of times instead of forever.

    The respawn re-resolves the role from spawn_args_from_record rather than pinning the recorded
    digest the way do_revive does: a birth-zombie never ran a turn, so this is a retry of the BIRTH,
    not the restoration of an agent that died holding a doctrine.
    """
    reaps = int(rr.get("zombie_reaps", 0) or 0)
    if reaps >= ZOMBIE_REAP_MAX:
        return False, "zombie-reap-cap (%d)" % reaps
    _, sess, tab = C.parse_agent_id(aid)
    e = tmux_env()
    roster = load_roster()
    cur = roster.get(aid, rr)
    cur["retired"] = True
    cur["retire_reason"] = "zombie-no-session"
    cur["retired_ts"] = time.time()
    cur["zombie_reaps"] = reaps + 1
    cur["session_id"] = ""  # never resume a session that never started
    cur.pop("recorder", None)
    roster[aid] = cur
    save_roster(roster)
    C.http_json("POST", QUEUE_URL + "/agents/unregister", {"agent_id": aid}, HDR, timeout=6)
    close_agent_window(sess, tab, e)
    # drop stale starting status so the next spawn's readiness wait is honest
    write_status(aid, "starting", cur.get("backend", "claude"), drop_session=True)
    latest = load_roster().get(aid, cur)
    rc = do_spawn(spawn_args_from_record(aid, latest))
    if rc != 0:
        return False, "respawn-failed rc=%s" % rc
    st = read_agent_status(aid)
    sid = (st.get("session_id") or "").strip()
    # Accept TUI-ready even without session_id (Claude often writes sid on first prompt).
    if not sid and not pane_looks_ready(aid):
        return False, "respawned-still-not-ready"
    if latest.get("owner_task_id"):
        C.send_when_ready(
            C.tmux_target(aid),
            "Recovered after birth-zombie (empty session_id / stuck starting). "
            "Read owner task %s on the TODO board, post a concise recovery status, "
            "and continue from the card context." % latest["owner_task_id"], 60)
    return True, "respawned session_id=%s reaps=%d" % (sid or "(pending-prompt)", reaps + 1)


def do_stop_recorders(argv):
    """Stop live asciinema recorders for local agents; clear roster.recorder. Agents stay up.

    The relief valve for a fleet already recording: each recorder holds a read-only tmux client,
    and a pane whose only client is read-only cannot be sent keys.
    """
    roster = load_roster()
    stopped = 0
    for aid, rr in sorted(roster.items()):
        if not is_local(aid):
            continue
        _, sess, tab = C.parse_agent_id(aid)
        if not sess or not tab:
            continue
        meta = rr.get("recorder")
        cur = C.current_recorder(sess, tab, HOST_ID)
        if not meta and not cur:
            if not C.recorder_identity(sess, tab, HOST_ID):
                continue
        info = C.stop_recorder(sess, tab, meta or cur, HOST_ID)
        if info or cur or meta:
            stopped += 1
        rr["recorder_stop"] = info
        rr.pop("recorder", None)
        # Also clear the intent, so the next spawn does not re-attach. --keep-intent stops the
        # running processes but leaves the agent opted in.
        if "--keep-intent" not in argv:
            rr["record"] = False
    save_roster(roster)
    print("stopped recorders on %d local agent(s); agents left running" % stopped)
    return 0


def do_reconcile(argv):
    """Restore agents whose windows disappeared without an intentional mp kill.

    Intent is durable: any retirement reason other than an observed window/server failure is a
    deliberate stop and is never restarted here. Accidentally lost agents resume their persisted
    backend session; if the session was never persisted, respawn the same roster identity.

    Also reaps birth-zombies (card 157dcb7c75): live window, empty session_id, status stuck at
    starting longer than ZOMBIE_STARTING_SEC -- kill + fresh spawn + owner-task brief.
    """
    windows = C.tmux_windows()
    if windows is None:
        print("reconcile skipped: tmux is not answering")
        return 0
    roster = load_roster()
    recovered, failed, zombies = [], [], []
    now = time.time()
    for aid, rr in sorted(roster.items()):
        if not is_local(aid):
            continue
        if rr.get("is_master"):
            continue
        _, sess, tab = C.parse_agent_id(aid)
        if not sess or not tab:
            continue

        # --- birth-zombie path (window present, backend never bootstrapped) ---
        if not rr.get("retired") and is_birth_zombie(aid, rr, now=now):
            last_attempt = float(rr.get("recovery_attempt_ts", 0) or 0)
            if now - last_attempt < 30:
                continue
            roster = load_roster()
            rr = roster.get(aid, rr)
            rr["recovery_attempt_ts"] = now
            rr["recovery_attempts"] = int(rr.get("recovery_attempts", 0) or 0) + 1
            save_roster(roster)
            ok, detail = reap_birth_zombie(aid, rr)
            if ok:
                recovered.append(aid)
                zombies.append("%s (%s)" % (aid, detail))
            else:
                failed.append(aid)
                zombies.append("%s FAILED %s" % (aid, detail))
            roster = load_roster()
            continue

        if "mc-%s:%s" % (sess, tab) in windows:
            continue
        # The roster holds every agent ever retired. Asking tmux and rereading the 700 KB roster for
        # each of those, every 15 s, cost more than the whole live fleet.
        if rr.get("retired") and rr.get("retire_reason", "") not in ACCIDENTAL_REASONS:
            continue
        if window_exists(sess, tab):  # started after the snapshot above
            continue
        # This pass's snapshot can predate an `mp kill` made while an earlier agent was being
        # respawned (seconds each); deciding on it revived killed agents. mp kill retires before
        # it closes the window, so the roster read now already says so.
        roster = load_roster()
        rr = roster.get(aid)
        if not rr:
            continue
        reason = rr.get("retire_reason", "")
        if rr.get("retired") and reason not in ACCIDENTAL_REASONS:
            continue
        last_attempt = float(rr.get("recovery_attempt_ts", 0) or 0)
        if now - last_attempt < 30:
            continue
        rr["recovery_attempt_ts"] = now
        rr["recovery_attempts"] = int(rr.get("recovery_attempts", 0) or 0) + 1
        if not rr.get("retired"):
            rr["retired"] = True
            rr["retire_reason"] = "window-missing"
            rr["retired_ts"] = now
        save_roster(roster)

        rc = do_revive([aid])
        fresh = False
        if rc != 0:
            latest = load_roster().get(aid, rr)
            rc = do_spawn(spawn_args_from_record(aid, latest))
            fresh = (rc == 0)
        if rc == 0:
            recovered.append(aid)
            if fresh and rr.get("owner_task_id"):
                # A resumed agent still has its context; a FRESH one lost it, so only that one
                # needs pointing back at its card. readiness is already gated inside do_spawn.
                C.send_when_ready(C.tmux_target(aid),
                    "Recovered after a tmux crash. Read owner task %s on the TODO board, post a "
                    "concise recovery status, and continue from the card context." %
                    rr["owner_task_id"], 60)
        else:
            failed.append(aid)
        roster = load_roster()
    print("reconciled recovered=%d failed=%d zombies=%d" %
          (len(recovered), len(failed), len(zombies)))
    for aid in recovered:
        print("recovered %s" % aid)
    for z in zombies:
        print("zombie %s" % z)
    for aid in failed:
        print("FAILED %s" % aid, file=sys.stderr)
    return 1 if failed else 0


def role_tag(aid, a, roster):
    """The personality an agent was actually born with -- read from the durable roster.

    A legacy, bundle-less agent renders as "-" rather than blank: an unmounted agent must be
    VISIBLY unmounted, never silently indistinguishable from a mounted one.
    """
    rr = roster.get(aid) or {}
    return rr.get("role_ref") or a.get("role_ref") or "-"


def live_run(aid, rr):
    """(model, effort) a Claude session last answered with, read from its own transcript.

    Launch flags miss a /model or /effort typed in the session; every reply records both. Either
    part is None when the transcript cannot say, and the caller falls back to the launch flags.
    """
    _, sess, tab = C.parse_agent_id(aid)
    sid = (C.read_json(os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab), {}) or {}).get(
        "session_id") or rr.get("session_id")
    paths = session_files("claude", sid) if sid else []
    if not paths:
        return None, None
    # ponytail: reads the last 2 MB only; a reply further back than that reads as the launch flags.
    with open(max(paths, key=os.path.getmtime), "rb") as f:
        f.seek(max(0, os.fstat(f.fileno()).st_size - (2 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d, dict) and isinstance(d.get("message"), dict) else {}
        if d.get("type") == "assistant" and not d.get("isSidechain") and not str(m.get("model", "<")).startswith("<"):
            return m["model"], d.get("effort")
    return None, None


