def do_revive(argv):
    if not argv:
        print(USAGE["revive"], file=sys.stderr); return 2
    aid = full_aid(argv[0])
    # `--model <id>` brings the same session back on another model; the respawn records it as a pin,
    # so later revives keep it. Without a pin the revive takes today's default: a resumed Claude
    # session otherwise keeps the model it last ran.
    model = argv[argv.index("--model") + 1] if "--model" in argv[1:-1] else None
    if not is_local(aid):
        ok, result = remote_task("revive", aid, {}, timeout=150)
        print(result)
        return 0 if ok else 1
    roster = load_roster()
    rr = roster.get(aid)
    if not rr:
        print("unknown agent %s" % aid, file=sys.stderr); return 1
    if not rr.get("retired"):
        print("ERROR: %s is not retired" % aid, file=sys.stderr); return 1
    sid = rr.get("session_id", "")
    if not sid:
        _, sess, tab = C.parse_agent_id(aid)
        status = C.read_json(os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab), {}) or {}
        sid = status.get("session_id", "")
    backend = rr.get("backend", "claude")
    transcripts = session_files(backend, sid) if sid else []
    if not sid or not transcripts:
        print("ERROR: %s is not resumable: persisted %s session transcript is missing" %
              (aid, backend),
              file=sys.stderr)
        return 1
    args = [aid, "--backend", backend]
    if rr.get("boss_id"):
        args += ["--boss", rr["boss_id"]]
    if rr.get("cwd"):
        args += ["--cwd", rr["cwd"]]
    if rr.get("is_master"):
        args += ["--master"]
    role = _record_role(rr)
    if role:
        args += ["--role", role]
    model = model or (rr.get("model") if rr.get("model_pinned") else None)
    if model:
        args += ["--model", model]
    # A revived owner must come back bound to the same card (see spawn_args_from_record).
    if rr.get("lifecycle") == "owner" and rr.get("owner_task_id"):
        args += ["--owner-task", rr["owner_task_id"]]
    elif rr.get("lifecycle") == "temporary":
        args += ["--temporary"]
    if rr.get("trust") == "community":
        args += ["--community"]   # a revive never hands an outsider's engineer its powers back
    # Pin the exact recorded ref+digest so a restored agent cannot silently come back under a
    # different doctrine than the one it died with. A node that updates itself in place (the cloud
    # image sets MP_REVIVE_FOLLOWS_REGISTRY=1) changes doctrine ON PURPOSE: there the revive resumes
    # the same session under the role the installed release ships, instead of refusing -- which
    # left the Boss unrevivable after any update that bumped its role.
    if os.environ.get("MP_REVIVE_FOLLOWS_REGISTRY") == "1":
        return do_spawn(args, resume_session=sid)
    return do_spawn(args, resume_session=sid, locked_role_ref=rr.get("role_ref") or None,
                    expected_role_digest=rr.get("role_digest") or None)


