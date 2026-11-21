# ---------- tcc: who makes macOS ask "MyPlow would like to access data from other apps" (card 1c417d9f90) ----------
# A process MyPlow started that opens another app's container (~/Library/Containers/<app>,
# ~/Library/Group Containers/<group>) makes sandboxd ask tccd for App Data, and the owner's answer
# lasts that one process only: a script started every minute asks every minute. `mp tcc serve`
# reads tccd's own log and writes one line per request. The asking process is still blocked on the
# request when its line arrives, so its parent chain and AGENT_ID are read live. tccd keeps the
# target <private>, so the container comes from the chain's argv or the process's open files, "?"
# when neither names one. Read-only: it never answers a prompt or touches TCC.
TCC_LOG = os.path.join(INSTALL_DIR, "logs", "tcc.log")
TCC_SERVICE = "kTCCServiceSystemPolicyAppData"
TCC_STREAM = ["/usr/bin/log", "stream", "--style", "ndjson", "--info", "--debug", "--predicate",
              'subsystem == "com.apple.TCC" AND category == "access" AND eventMessage BEGINSWITH "AUTHREQ_"']
TCC_CONTAINER = re.compile(r"Library/(?:Group\\? Containers|Containers)/([A-Za-z0-9._-]+)")
TCC_VALUES = {"0": "denied", "2": "allowed", "3": "limited"}  # tccd's authValue
TCC_USAGE = ("usage: mp tcc [YYYY-MM-DD]   (macOS App Data requests that day, by agent and container;\n"
             "                              one line each in logs/tcc.log, recorded by mp tcc serve)")


def tcc_requests(lines, snapshot):
    """tccd's ndjson log lines -> one dict per App Data request, yielded when tccd answers it.
    snapshot(pid) runs on the attribution line, while the asking process still waits."""
    pending = {}
    for line in lines:
        try:
            ev = json.loads(line)
            msg = ev.get("eventMessage") or ""
        except (ValueError, AttributeError):
            continue
        m = re.match(r"(AUTHREQ_\w+): msgID=([\w.]+)", msg)
        if not m:
            continue
        kind, mid = m.groups()
        if kind == "AUTHREQ_CTX" and "service=%s," % TCC_SERVICE in msg:
            pending[mid] = {"ts": ev.get("timestamp", ""), "prompt": False}
            if len(pending) > 200:  # a request whose answer the stream dropped
                pending.pop(next(iter(pending)))
            continue
        req = pending.get(mid)
        if req is None:
            continue
        if kind == "AUTHREQ_ATTRIBUTION":
            a = re.search(r"accessing=\{TCCDProcess: [^}]*?pid=(\d+)[^}]*?binary_path=([^,}]+)", msg)
            if a:
                req.update(pid=int(a.group(1)), binary=a.group(2))
                req.update(snapshot(req["pid"]))
        elif kind == "AUTHREQ_PROMPTING":
            req["prompt"] = True
        elif kind == "AUTHREQ_RESULT":
            v = re.search(r"authValue=(\d+)", msg)
            req["result"] = TCC_VALUES.get(v.group(1) if v else "", "unknown")
            yield pending.pop(mid)


def _ps(*args):
    """{pid: rest of the row} from `ps <args>`, whose first column is the pid."""
    out = subprocess.run(["ps"] + list(args), capture_output=True, text=True, timeout=10).stdout
    rows = (r.strip().split(None, 1) for r in out.splitlines())
    return {int(r[0]): r[1] for r in rows if len(r) == 2}


def tcc_snapshot(pid):
    """{chain, agent, container} of a live process: its ancestors up to the last one carrying the
    nearest AGENT_ID (to launchd when none does), and the first container their argv names, else
    the process's open files or cwd, else its script's source. {} once the process is gone.
    Only executable and script names are kept: an argv can carry a secret."""
    parent = {p: int(pp) for p, pp in _ps("-axo", "pid=,ppid=").items()}
    pids = [pid]
    while parent.get(pids[-1], 1) > 1 and len(pids) < 30:
        pids.append(parent[pids[-1]])
    plist = ",".join(map(str, pids))
    args, envs = _ps("-o", "pid=,args=", "-p", plist), _ps("-E", "-o", "pid=,args=", "-p", plist)
    if pid not in args:
        return {}
    agents = []
    for p in pids:  # ps -E prints the environment after the argv
        a = re.search(r"(?:^| )AGENT_ID=(\S+)", envs.get(p, "")[len(args.get(p, "")):])
        agents.append(a.group(1) if a else "")
    agent = next((a for a in agents if a), "")
    keep = len(pids) - agents[::-1].index(agent) if agent else len(pids)
    chain, scripts = [], []
    for p in pids[:keep]:
        words = args.get(p, "?").split()
        scripts.append(next((w for w in words[1:4] if re.search(r"\.(py|sh|mjs|js)$", w)), ""))
        chain.append(os.path.basename(words[0]).lstrip("-")
                     + (" " + os.path.basename(scripts[-1]) if scripts[-1] else ""))
    c = next(filter(None, (TCC_CONTAINER.search(args.get(p, "")) for p in pids)), None)
    if not c:
        files = subprocess.run(["lsof", "-nP", "-p", str(pid), "-Ffn"], capture_output=True,
                               text=True, timeout=10).stdout
        c = TCC_CONTAINER.search(files)
        cwd = re.search(r"^fcwd\nn(.*)$", files, re.M)
        if not c and scripts[0] and cwd:  # e.g. python3 watcher.py, whose source names the path
            try:
                with open(os.path.join(cwd.group(1), scripts[0]), errors="replace") as f:
                    c = TCC_CONTAINER.search(f.read(1 << 20))
            except OSError:
                pass
    return {"chain": " <- ".join(chain), "agent": agent, "container": c.group(1) if c else ""}


def tcc_line(req):
    """One request -> one tab-separated line: time, agent, container, prompt?, answer, chain."""
    return "\t".join([req["ts"][:19] + " " + time.strftime("%Z"), req.get("agent") or "-",
                      req.get("container") or "?", "prompt" if req["prompt"] else "no-prompt",
                      req.get("result", "unknown"),
                      req.get("chain") or "%s (exited)" % os.path.basename(req.get("binary", "?"))]
                     ) + "\n"


def tcc_counts(lines, day):
    """(prompts, requests) per (agent, container) on `day`, most prompts first."""
    counts = {}
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) >= 4 and f[0].startswith(day):
            c = counts.setdefault((f[1], f[2]), [0, 0])
            c[0] += f[3] == "prompt"
            c[1] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))


def do_tcc(argv):
    """mp tcc [YYYY-MM-DD] | mp tcc serve — App Data requests by agent and container (card 1c417d9f90)."""
    if argv == ["serve"]:
        import fcntl
        lock = open(os.path.join(INSTALL_DIR, "run", "tcc.lock"), "a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("mp tcc serve is already running", file=sys.stderr); return 0
        os.makedirs(os.path.dirname(TCC_LOG), exist_ok=True)

        def snapshot(pid):
            try:
                return tcc_snapshot(pid)
            except Exception as e:  # the process is gone, or ps hung: keep the request anyway
                return {"chain": "(%r)" % e}
        while True:
            stream = subprocess.Popen(TCC_STREAM, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True)
            for req in tcc_requests(stream.stdout, snapshot):
                with open(TCC_LOG, "a") as f:
                    f.write(tcc_line(req))
            stream.wait()
            time.sleep(10)  # logd restarted, or this is not a Mac
    if len(argv) > 1 or (argv and not re.match(r"\d{4}-\d\d-\d\d$", argv[0])):
        print(TCC_USAGE, file=sys.stderr); return 2
    day = argv[0] if argv else time.strftime("%Y-%m-%d")
    try:
        with open(TCC_LOG) as f:
            rows = tcc_counts(f, day)
    except FileNotFoundError:
        rows = []
    print("%s: %d App Data prompts, %d requests (prompts/requests, agent, container)"
          % (day, sum(c[0] for _, c in rows), sum(c[1] for _, c in rows)))
    for (agent, container), (prompts, total) in rows:
        print("  %4d/%-4d %s  %s" % (prompts, total, agent, container))
    return 0


