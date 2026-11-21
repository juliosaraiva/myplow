# ---------- cron: a schedule is one `mp send` on a timer (card 0176b7cafd) ----------
# CEO: "Cron = literally a scheduled mp send <agent> "<message>" (any message, any agent)". A fire
# runs exactly that command, so it keeps the send's readiness guard and its routing to remote
# hosts. The schedules (written only by add/rm) and the fire record (written only by serve) are
# separate files, so neither writer can resurrect or erase what the other just changed.
CRON_PATH = os.path.join(INSTALL_DIR, "run", "cron.json")
CRON_STATE_PATH = os.path.join(INSTALL_DIR, "run", "cron-state.json")
CRON_LOG = os.path.join(INSTALL_DIR, "logs", "cron.log")
# A busy or restarting agent is retried this long. Past it that fire is skipped, as cron skips
# the minutes a machine slept through.
CRON_RETRY_MIN = 10
CRON_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0"}
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CRON_USAGE = ('usage: mp cron add "<minute hour day month weekday>" <session:agent> "<task>" '
              '[--for <name>] [--reply "<reply command>"]\n'
              '         (standard cron or @hourly/@daily/@weekly, in this machine\'s local time;\n'
              '          --for is who asked, --reply how to answer them in the channel they asked from)\n'
              '       mp cron set <id> [--for <name>] [--reply "<reply command>"]\n'
              '       mp cron ls\n'
              '       mp cron rm <id>')


def cron_parse(expr):
    """Standard 5-field cron -> [minutes, hours, days, months, weekdays, day_any, weekday_any].
    Raises ValueError, so a bad schedule is refused when it is added instead of never firing."""
    fields = CRON_ALIASES.get(expr.strip(), expr).split()
    if len(fields) != 5:
        raise ValueError("need 5 fields: minute hour day-of-month month day-of-week")
    sets = []
    for field, (lo, hi) in zip(fields, CRON_RANGES):
        vals = set()
        for part in field.split(","):
            rng, slash, step = part.partition("/")
            if rng == "*":
                a, b = lo, hi
            elif "-" in rng:
                a, b = (int(x) for x in rng.split("-", 1))
            else:
                a = int(rng)
                b = hi if slash else a
            if not lo <= a <= b <= hi or (slash and int(step) < 1):
                raise ValueError("%r is outside %d-%d" % (part, lo, hi))
            vals.update(range(a, b + 1, int(step) if slash else 1))
        sets.append(vals)
    if 7 in sets[4]:
        sets[4].add(0)  # 0 and 7 are both Sunday
    return sets + [fields[2].startswith("*"), fields[4].startswith("*")]


def cron_match(parsed, t):
    """Does local minute `t` match? Days follow cron: when BOTH day fields are restricted, either
    one is enough ("0 9 1 * 1" = the 1st of the month AND every Monday)."""
    mins, hours, days, months, wdays, day_any, wday_any = parsed
    if t.minute not in mins or t.hour not in hours or t.month not in months:
        return False
    day_ok, wday_ok = t.day in days, t.isoweekday() % 7 in wdays
    return (day_ok and wday_ok) if (day_any or wday_any) else (day_ok or wday_ok)


def cron_due(parsed, created, now):
    """The latest scheduled minute in the retry window that is not older than the schedule, or None."""
    from datetime import timedelta
    now = now.replace(second=0, microsecond=0)
    for back in range(CRON_RETRY_MIN):
        m = now - timedelta(minutes=back)
        if m.timestamp() < created // 60 * 60:
            return None
        if cron_match(parsed, m):
            return m
    return None


def cron_package(s):
    """What the agent receives (CEO 10-16): who set the task up, and how to answer them in the
    channel they asked from, saved when the schedule was made."""
    who = s.get("for") or "the owner"
    reply = ("\nWhen done, reply to %s here: %s" % (who, s["reply"])) if s.get("reply") else ""
    return "[cronjob] Task %s set up: %s%s" % (who, s["message"], reply)


def cron_fire(s):
    """Run `mp send` with the schedule's package. It goes on stdin, so no argv rule or length
    limit can mangle it; AGENT_ID is dropped because the answer goes to --reply, not to a sender."""
    env = dict(os.environ)
    env.pop("AGENT_ID", None)
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "send", s["agent"]],
                           input=cron_package(s), capture_output=True, text=True, timeout=180,
                           env=env)
        return r.returncode, (r.stdout.strip() or r.stderr.strip())[-200:]
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _cron_flags(rest):
    """Pull --for/--reply <value> out of argv: ({field: value}, the remaining args)."""
    got = {}
    for flag, field in (("--for", "for"), ("--reply", "reply")):
        if flag in rest:
            i = rest.index(flag)
            got[field] = rest[i + 1] if i + 1 < len(rest) else ""
            rest = rest[:i] + rest[i + 2:]
    return got, rest


def cron_tick(now):
    """Fire every schedule due at `now` that has not already been delivered for that minute."""
    schedules = C.read_json(CRON_PATH, {}) or {}
    state = C.read_json(CRON_STATE_PATH, {}) or {}
    changed = set(state) - set(schedules)
    for sid, s in sorted(schedules.items()):
        try:
            due = cron_due(cron_parse(s["when"]), s.get("created", 0), now)
        except (KeyError, ValueError):
            continue
        if due is None:
            continue
        key, rec = due.strftime("%Y-%m-%d %H:%M"), state.get(sid) or {}
        if rec.get("sent") == key:
            continue
        rc, out = cron_fire(s)
        state[sid] = dict(rec, last=key, result=out, sent=key if rc == 0 else rec.get("sent"))
        changed.add(sid)
        os.makedirs(os.path.dirname(CRON_LOG), exist_ok=True)
        with open(CRON_LOG, "a") as f:
            f.write("%s cron %s (%s) -> mp send %s: %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S %Z"), sid, key, s["agent"], out))
    if changed:
        C.write_json(CRON_STATE_PATH, {k: v for k, v in state.items() if k in schedules})


def do_cron(argv):
    """mp cron add|set|ls|rm|serve — schedules that each run one `mp send` (card 0176b7cafd)."""
    from datetime import datetime
    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    flags, rest = _cron_flags(rest)
    if sub == "add" and len(rest) >= 3:
        when, agent, msg = rest[0], rest[1], " ".join(rest[2:]).strip()
        try:
            cron_parse(when)
        except ValueError as e:
            print("ERROR: bad schedule %r: %s" % (when, e), file=sys.stderr); return 2
        # A bare name resolves to no pane, so the schedule would fail every time it fired.
        if ":" not in agent:
            print("ERROR: name the agent as session:name, e.g. main:%s (as mp status shows it)"
                  % agent, file=sys.stderr); return 2
        if not msg:
            print(CRON_USAGE, file=sys.stderr); return 2
        sid = os.urandom(4).hex()
        schedules = C.read_json_tracked(CRON_PATH, {}) or {}
        schedules[sid] = dict(flags, when=when, agent=agent, message=msg, created=time.time(),
                              by=os.environ.get("AGENT_ID", "").strip() or "CEO")
        C.write_json_merged(CRON_PATH, schedules)
        print("cron %s: %s -> mp send %s %s"
              % (sid, when, agent, shlex.quote(cron_package(schedules[sid]))))
        return 0
    if sub == "set" and len(rest) == 1 and flags:
        schedules = C.read_json_tracked(CRON_PATH, {}) or {}
        if rest[0] not in schedules:
            print("ERROR: no schedule %s (mp cron ls)" % rest[0], file=sys.stderr); return 1
        schedules[rest[0]] = dict(schedules[rest[0]], **flags)
        C.write_json_merged(CRON_PATH, schedules)
        print("cron %s now sends: %s" % (rest[0], shlex.quote(cron_package(schedules[rest[0]]))))
        return 0
    if sub == "rm" and len(rest) == 1:
        schedules = C.read_json_tracked(CRON_PATH, {}) or {}
        if schedules.pop(rest[0], None) is None:
            print("ERROR: no schedule %s (mp cron ls)" % rest[0], file=sys.stderr); return 1
        C.write_json_merged(CRON_PATH, schedules)
        print("removed %s" % rest[0])
        return 0
    if sub == "ls" and not rest:
        state = C.read_json(CRON_STATE_PATH, {}) or {}
        for sid, s in sorted((C.read_json(CRON_PATH, {}) or {}).items()):
            rec = state.get(sid) or {}
            print("%s  %-14s %-16s last: %s %s  by %s\n          %s"
                  % (sid, s["when"], s["agent"], rec.get("last", "never"), rec.get("result", ""),
                     s.get("by", ""), shlex.quote(cron_package(s))))
        return 0
    if sub == "serve" and not rest:
        import fcntl
        lock = open(os.path.join(INSTALL_DIR, "run", "cron.lock"), "a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("mp cron serve is already running", file=sys.stderr); return 0
        while True:
            try:
                cron_tick(datetime.now())
            except Exception as e:  # one bad tick must not stop every schedule for good
                print("%s cron tick failed: %r" % (time.strftime("%FT%T"), e), file=sys.stderr)
            time.sleep(15 - time.time() % 15)  # ticks on :00 :15 :30 :45, so a fire lands at :00
    print(CRON_USAGE, file=sys.stderr)
    return 2


