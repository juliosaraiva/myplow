# ---------------- watchdog deferred-job facility ----------------
def _wcfg(k, d):
    v = os.environ.get(k)
    if v is None:
        v = CFG.get(k)
    return v if (v is not None and v != "") else d

WATCHDOG_AGENT = _wcfg("WATCHDOG_AGENT", "%s/watchdog:Watchdog" % HOST_ID)
WATCHDOG_NUDGE_DELAY_MIN = float(_wcfg("WATCHDOG_NUDGE_DELAY_MIN", 3))
WATCHDOG_TASKCREATE_DELAY_MIN = float(_wcfg("WATCHDOG_TASKCREATE_DELAY_MIN", 10))
WATCHDOG_MAX_LOAD = float(_wcfg("WATCHDOG_MAX_LOAD", 0))   # 0 = disabled
WATCHDOG_POLL_SEC = float(_wcfg("WATCHDOG_POLL_SEC", 5))
# A failed mp_send is retried with linear backoff (60s, 120s, ... 600s => ~55min horizon).
# Past that the job is dropped with an `abandoned` log line: a nudge older than ~1h is noise,
# but the loss is recorded rather than silent.
WATCHDOG_RETRY_BACKOFF_SEC = float(_wcfg("WATCHDOG_RETRY_BACKOFF_SEC", 60))
WATCHDOG_MAX_ATTEMPTS = int(float(_wcfg("WATCHDOG_MAX_ATTEMPTS", 10)))
JOBS_PATH = os.path.join(TODOS_DIR, "watchdog-jobs.json")
JOBS_LOG = os.path.join(TODOS_DIR, "watchdog-jobs.log")
WD_PAUSE = os.path.join(TODOS_DIR, "watchdog.PAUSE")


def wd_new_store():
    return {"version": 1, "jobs": []}


def wd_log(event, job, extra=""):
    try:
        line = "%s %s card=%s kind=%s %s\n" % (time.strftime("%FT%TZ", time.gmtime()),
                                                event, job.get("card"), job.get("kind"), extra)
        with open(JOBS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def wd_load_store():
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if not isinstance(st, dict) or not isinstance(st.get("jobs"), list):
            return wd_new_store()
        good = []
        for j in st["jobs"]:
            if (isinstance(j, dict) and j.get("card")
                    and isinstance(j.get("fire_at"), (int, float))
                    and j.get("kind") in ("unanswered", "taskcreate")):
                good.append(j)
        st["jobs"] = good
        return st
    except Exception:
        return wd_new_store()


def wd_save_store(store):
    os.makedirs(TODOS_DIR, exist_ok=True)
    C.atomic_write(JOBS_PATH, json.dumps(store, ensure_ascii=False).encode())


def _wd_is_ceo_or_watchdog(by):
    return by == "CEO" or by == WATCHDOG_AGENT


def wd_schedule_unanswered(tid, comment_id, by):
    """Caller holds LOCK. CEO/Watchdog comment => schedule, superseding any pending job on this card."""
    if not _wd_is_ceo_or_watchdog(by):
        return
    store = wd_load_store()
    store["jobs"] = [j for j in store["jobs"] if not (j["card"] == tid and j["kind"] == "unanswered")]
    job = {"id": uuid.uuid4().hex[:8], "card": tid, "comment_id": comment_id, "by": by,
           "fire_at": now() + WATCHDOG_NUDGE_DELAY_MIN * 60, "kind": "unanswered"}
    store["jobs"].append(job)
    wd_save_store(store)
    wd_log("scheduled", job, "by=%s" % by)


def wd_schedule_taskcreate(tid):
    """Caller holds LOCK. Any new task => schedule an owner-hasn't-spoken gate."""
    store = wd_load_store()
    store["jobs"] = [j for j in store["jobs"] if not (j["card"] == tid and j["kind"] == "taskcreate")]
    job = {"id": uuid.uuid4().hex[:8], "card": tid, "comment_id": None, "by": "CEO",
           "fire_at": now() + WATCHDOG_TASKCREATE_DELAY_MIN * 60, "kind": "taskcreate"}
    store["jobs"].append(job)
    wd_save_store(store)
    wd_log("scheduled", job, "taskcreate")


def wd_gate_holds(task, job):
    if task is None:
        return False
    comments = task.get("comments") or []
    if job["kind"] == "unanswered":
        if not comments:
            return False
        return _wd_is_ceo_or_watchdog(comments[-1].get("by"))
    # taskcreate: fire when the card's OWNER still hasn't said the first word. Covers both
    # "nobody picked it up" (no assignee) and "assigned but silent" -- the latter is the real
    # failure and a comments==0 gate never catches it, because Boss triages in seconds.
    # Terminal cards are exempt: a done/cancelled card needs no nudge.
    if task.get("state") in TERMINAL_STATES:
        return False
    assignee = task.get("assignee") or ""
    if not assignee:
        return True
    return not any(c.get("by") == assignee for c in comments)


def wd_incident_text(job, task):
    if job["kind"] == "unanswered":
        quoted = ""
        for c in reversed(task.get("comments") or []):
            if c.get("id") == job.get("comment_id"):
                quoted = c.get("body", ""); break
        if not quoted and task.get("comments"):
            quoted = task["comments"][-1].get("body", "")
        return "[watchdog incident] card=%s unanswered by=%s: %s" % (job["card"], job["by"], quoted)
    # Two distinct failures reach here. The "new task unowned" wording is verbatim on purpose:
    # the Watchdog persona keys its nudge off that exact phrase.
    owner = task.get("assignee") or ""
    if owner:
        return ("[watchdog incident] card=%s owner assigned but silent: %s has owned this card and has "
                "not posted a first message. Tell them to REPLY ON THIS CARD now: %s"
                % (job["card"], owner, (task.get("text", "") or "")))
    return "[watchdog incident] card=%s new task unowned: %s" % (job["card"], (task.get("text", "") or ""))


def wd_resolve_due(store, board, now_ts):
    """Remove ALL due jobs (fired or cancelled) BEFORE dispatch => fire-once + idempotent under concurrent scans."""
    fire, remaining = [], []
    for j in store["jobs"]:
        if j["fire_at"] > now_ts:
            remaining.append(j)
            continue
        task = (board.get("tasks") or {}).get(j["card"])
        if wd_gate_holds(task, j):
            fire.append(j)
        else:
            wd_log("cancelled", j, "gate-failed")
    store["jobs"] = remaining
    return fire


def watchdog_worker():
    sys.stderr.write("watchdog-worker started (delay=%smin poll=%ss agent=%s)\n"
                     % (WATCHDOG_NUDGE_DELAY_MIN, WATCHDOG_POLL_SEC, WATCHDOG_AGENT))
    while True:
        try:
            time.sleep(WATCHDOG_POLL_SEC)
            if os.path.exists(WD_PAUSE):
                continue
            if WATCHDOG_MAX_LOAD:
                try:
                    if os.getloadavg()[0] > WATCHDOG_MAX_LOAD:
                        continue
                except Exception:
                    pass
            # cheap pre-check WITHOUT the board or the lock: is anything actually due?
            store = wd_load_store()
            if not any(j["fire_at"] <= now() for j in store["jobs"]):
                continue
            board = load_board()   # heavy board read done OUTSIDE the LOCK, and only when due
            with LOCK:             # hold the LOCK only for the tiny store mutation (never the board read)
                store = wd_load_store()
                fire = wd_resolve_due(store, board, now())
                wd_save_store(store)
            requeue = []
            for j in fire:
                task = (board.get("tasks") or {}).get(j["card"])
                if task is None:
                    continue
                rc = mp_send(WATCHDOG_AGENT, wd_incident_text(j, task))
                attempts = j.get("attempts", 0) + 1
                j["attempts"] = attempts
                if rc == 0:
                    wd_log("fired", j, "mp_send_rc=0 attempts=%s" % attempts)
                    continue
                # Jobs are popped from the store BEFORE dispatch (fire-once), so a failed send would
                # die right here, silently. Re-arm it with linear backoff instead; the gate is
                # re-checked on the next due pass, so a nudge that became stale cancels itself.
                if attempts >= WATCHDOG_MAX_ATTEMPTS:
                    wd_log("abandoned", j, "mp_send_rc=%s attempts=%s" % (rc, attempts))
                    continue
                delay = WATCHDOG_RETRY_BACKOFF_SEC * attempts
                j["fire_at"] = now() + delay
                requeue.append(j)
                wd_log("retry", j, "mp_send_rc=%s attempts=%s next_in=%ss" % (rc, attempts, int(delay)))
            if requeue:
                with LOCK:
                    store = wd_load_store()
                    store["jobs"].extend(requeue)
                    wd_save_store(store)
        except Exception as e:
            sys.stderr.write("watchdog_worker error: %r\n" % e)


