class _BoardWrite:
    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p == "/dashboard" or p in ("/agents", "/clients", "/roster", "/revive") or p.startswith("/task/"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        ok, ident = self._identity()
        if not ok:
            # Do not read the body. Close so a keep-alive client cannot have that unread
            # body parsed as its next request.
            if self._declared_length():
                self.close_connection = True
            return self._send(401, {"error": "unauthorized"})

        ctype = self.headers.get("Content-Type", "")
        upload = p == "/todo/proof" and ctype.startswith("multipart/form-data")
        if self._enforce_body_limit(C.max_upload_bytes() if upload else C.max_json_bytes()):
            return
        if upload:
            return self._proof_multipart()
        body = self._body()

        if p == "/todo/update":
            return self._update(body, ident)
        if p == "/todo/comment":
            return self._comment(body, ident)
        if p == "/todo/comment/delete":
            return self._comment_delete(body, ident)
        if p == "/todo/owner":
            return self._owner(body, ident)
        if p == "/todo/proof":
            return self._proof_json(body)
        if p == "/todo/status":
            return self._status_op(body, ident)
        if p == "/todo/boss":
            return self._boss_op(body, ident)
        if p == "/todo/plugins/set":
            try:
                mpplugins.set_on(str(body.get("name") or ""), body.get("on") is True)
            except mpplugins.PluginError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"plugins": mpplugins.listing()})
        if p in ("/todo/plugins/connect", "/todo/plugins/wrap", "/todo/plugins/wrap-ask"):
            if ident != "browser":  # the owner's click, as for Access below: a message can reach an agent, never this
                return self._send(403, {"error": "the owner does this in the app"})
            try:
                if p == "/todo/plugins/wrap":
                    mpplugins.save_rule(str(body.get("name") or ""), body.get("text"))
                elif p.endswith("connect"):
                    mpplugins.connect_ask(body.get("text"), self._ask_agent)
                else:
                    mpplugins.rule_ask(str(body.get("name") or ""), body.get("text"), self._ask_agent)
            except mpplugins.PluginError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"plugins": mpplugins.listing()})
        if p.startswith("/todo/access/"):
            # Who gets in and with what is the owner's click in the app. An agent holds the machine secret, and a peer's
            # message reaches an agent, so the machine secret must not be able to grant. ponytail: a local agent can still
            # write keys.json or take a loopback session; this stops a talked-into curl, not a determined local process.
            if ident != "browser":
                return self._send(403, {"error": "the owner does this in the app"})
            op, host = p.rsplit("/", 1)[1], str(body.get("host") or "")
            if op == "line":
                return self._access("line", str(body.get("name") or ""))
            if op == "grant" and body.get("both") is False:
                return self._access(op, host, "one-way")
            if op in ("grant", "decline", "remove"):
                return self._access(op, host)
            if op in ("save", "ask"):
                return self._access(op, host, str(body.get("text") or ""))
        return self._send(404, {"error": "not_found"})

    def _ask_agent(self, text, words):
        """An ask from the app, to the agent that owns this setup. The owner's words land on the Boss card once it has them:
        there the agent can confirm them, which no sender can fake, and its answer lands where the owner reads. A careful
        agent will not act on pasted text that merely says "your owner"."""
        with LOCK:
            cid = board_snapshot().get("bossChat")
        note = (
            (
                "\nThese words are on your owner's Boss card, written from their app. Confirm with mp card %s, and answer "
                'there: mp comment %s "..."' % (cid, cid)
            )
            if cid
            else ""
        )
        if mp_send(BOSS_AGENT, text + note) != 0:
            raise mpplugins.PluginError("not sent: your agent could not be reached")
        with LOCK:
            board = board_for_edit()
            t = task_for_edit(board, cid) if cid else None
            if t:
                t["comments"].append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "by": "CEO",
                        "to": BOSS_AGENT,
                        "kind": "comment",
                        "body": "From the Plugins page: " + words,
                        "ts": now(),
                    }
                )
                t["updated"] = now()
                save_board(board, changed=[cid])

    def _access(self, *op, extra=None):
        """The a2a plugin owns pairing state: the Access page asks it, as mp pair does."""
        try:
            r = subprocess.run([sys.executable, A2A, "access", *op], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return self._send(200, dict(json.loads(r.stdout), **(extra or {})))
            return self._send(400, {"error": (r.stderr or r.stdout).strip()[:300] or "failed"})
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return self._send(500, {"error": str(e)[:300]})

    # ---- Boss lifecycle control (card 0cc0bde980) ----
    def _boss_alive(self):
        """Is the singleton Boss up? Heartbeat-driven, so it can lag reality by one interval; it
        is a UX guard, not the invariant. `mp spawn`'s own window_exists check is the real backstop
        and queue-client dispatches serially, so a stale yes/no here cannot produce two Bosses."""
        code, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None, {"X-Queue-Secret": SECRET}, timeout=6)
        for a in agents or []:
            if a.get("is_master") and a.get("state") == "alive":
                return True, a.get("agent_id", "")
        return False, ""

    def _boss_op(self, body, ident):
        """Kill / spawn / revive the Boss from the board UI.

        Adds NO new execution mechanism: every action rides the queue-server task path that
        queue-client's dispatch() already implements against `mp` (type=kill with --reason,
        type=spawn with --master --backend, type=revive). This endpoint only chooses the
        arguments and enforces the exactly-one-Boss guard.

        spawn vs revive: spawn starts a NEW Boss on a chosen engine; revive restores the SAME
        Boss, resuming its persisted session (its memory). Both require the Boss to be down.
        """
        action = (body.get("action") or "").strip().lower()
        alive, live_id = self._boss_alive()

        if action == "kill":
            if not alive:
                return self._send(409, {"ok": False, "error": "boss_not_alive"})
            # A non-accidental reason is what makes the kill stick: mp records it as durable intent
            # in roster.json and ensure-boss refuses to respawn against it.
            payload = {"reason": "ceo-kill:%d" % int(time.time())}
            target = live_id or BOSS_AGENT
        elif action == "spawn":
            backend = (body.get("backend") or "").strip().lower()
            if backend not in BOSS_BACKENDS:
                return self._send(
                    400, {"ok": False, "error": "bad_backend", "detail": "choose %s" % ", ".join(BOSS_BACKENDS)}
                )
            if alive:
                return self._send(
                    409,
                    {
                        "ok": False,
                        "error": "boss_already_alive",
                        "agent_id": live_id,
                        "detail": "kill the running Boss before spawning another",
                    },
                )
            why = C.backend_unready(backend)
            if why:  # said here, on the card: queued, mp spawn's refusal reached only a log and the page said Down
                return self._send(409, {"ok": False, "error": "backend_not_ready", "detail": why})
            # role:boss is required or do_spawn refuses a bundle-less Boss; queue-client also
            # defaults it for any master spawn, but state it explicitly here (card f6339b85a2).
            payload = {"backend": backend, "is_master": True, "role": "boss"}
            target = BOSS_AGENT
        elif action == "revive":
            if alive:
                return self._send(
                    409,
                    {
                        "ok": False,
                        "error": "boss_already_alive",
                        "agent_id": live_id,
                        "detail": "the Boss is already up; nothing to revive",
                    },
                )
            rc, roster = C.http_json("GET", CFG["QUEUE_URL"] + "/roster", None, {"X-Queue-Secret": SECRET}, timeout=6)
            was = next(
                (
                    r.get("backend")
                    for r in (roster if rc == 200 and isinstance(roster, list) else [])
                    if r.get("agent_id") == BOSS_AGENT
                ),
                "",
            )
            why = C.backend_unready(was) if was in BOSS_BACKENDS else ""
            if why:
                return self._send(
                    409,
                    {
                        "ok": False,
                        "error": "backend_not_ready",
                        "detail": why + ". Or start a new Boss on another one.",
                    },
                )
            payload = {}
            target = BOSS_AGENT
        else:
            return self._send(400, {"ok": False, "error": "bad_action", "detail": "choose kill, spawn or revive"})

        code, r = C.http_json(
            "POST",
            CFG["QUEUE_URL"] + "/task/submit",
            {"type": action, "target_agent": target, "payload": payload},
            {"X-Queue-Secret": SECRET},
            timeout=10,
        )
        if code != 200 or not isinstance(r, dict) or not r.get("task_id"):
            return self._send(502, {"ok": False, "error": "submit_failed", "detail": str(r)[:200]})
        return self._send(200, {"ok": True, "action": action, "agent_id": target, "task_id": r["task_id"], "by": ident})

    # ---- board view (server-authoritative ordering: pinned first by pinRank, then order) ----
    def _terminal_graph(self):
        """Live fleet topology. Polling this metadata never replaces terminal iframe clients."""
        acode, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None, {"X-Queue-Secret": SECRET})
        rcode, roster = C.http_json("GET", CFG["QUEUE_URL"] + "/roster", None, {"X-Queue-Secret": SECRET})
        if acode != 200 or not isinstance(agents, list):
            agents = []
        if rcode != 200 or not isinstance(roster, list):
            roster = []
        roster_by_id = {r.get("agent_id", ""): r for r in roster}
        live_ids = {a.get("agent_id", "") for a in agents if a.get("state", "alive") == "alive"}
        sizes = _tmux_window_sizes()
        viewers = _tmux_viewers()
        nodes = []
        for a in agents:
            aid = a.get("agent_id", "")
            rr = roster_by_id.get(aid, {})
            if not aid or aid not in live_ids or rr.get("retired") is True:
                continue
            target = a.get("tmux_target") or C.tmux_target(aid)
            cols, rows = sizes.get(target, (0, 0))
            nodes.append(
                {
                    "agent_id": aid,
                    "boss_id": rr.get("boss_id") or a.get("boss_id", ""),
                    "is_master": bool(rr.get("is_master") or a.get("is_master")),
                    "state": a.get("status", "ready"),
                    "summary": a.get("summary", ""),
                    "target": target,
                    "host": rr.get("host") or a.get("host", ""),
                    "cols": cols or 160,
                    "rows": rows or 48,
                }
            )
            if viewers is not None:  # tmux turns . and : into _ in a session name
                nodes[-1]["viewers"] = viewers.get(target.split(":", 1)[-1].replace(".", "_"), 0)
        nodes.sort(key=lambda n: (not n["is_master"], n["agent_id"]))
        node_ids = {n["agent_id"] for n in nodes}
        edges = [{"from": n["boss_id"], "to": n["agent_id"]} for n in nodes if n["boss_id"] in node_ids]
        # Ports come from config, never constants: the tiles are iframes onto this install's own
        # ttyd, and a hardcoded port silently renders every tile dead wherever ttyd was moved.
        return {
            "nodes": nodes,
            "edges": edges,
            "readonly_port": int(CFG["TTYD_RO_PORT"]),
            "interactive_port": int(CFG["TTYD_BROWSER_PORT"]),
            "ts": now(),
            "version": "v" + C.version(),
        }

    def _board_view(self):
        board = load_board()
        return board

    # ---- update op ----
    def _update(self, body, ident):
        op = body.get("op")
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        with LOCK:
            board = board_for_edit()
            if op == "add":
                tid = uuid.uuid4().hex[:10]
                # assignee is NOT settable here: /todo/owner is the only door, and it is the one
                # that checks the agent is alive, unretired, this Boss's, and born for this card.
                task = {
                    "id": tid,
                    "text": body.get("text", ""),
                    "state": "working",
                    "assignee": "",
                    "ownerHistory": [],
                    "ownerNeedsReplacement": False,
                    "pinned": False,
                    "pinRank": None,
                    "doneCondition": "",
                    "done": False,
                    "verified": False,
                    "unread": 0,
                    "pingsToBoss": 0,
                    "comments": [],
                    "proofs": [],
                    "test": bool(body.get("test")),
                    "created": now(),
                    "updated": now(),
                }
                board["tasks"][tid] = task
                board["order"].insert(0, tid)
                # Count the ping before the only save, and tell the Boss after it commits.
                msg = take_task_event(task, "new task added")
                save_board(board, changed=[tid])
                if msg:
                    ping_boss(msg)
                # Every new card, regardless of who created it. Boss relays most of the CEO's
                # cards via the API (by=.../Boss), so an actor=="CEO" guard here would leave those
                # silently uncovered.
                wd_schedule_taskcreate(tid)
                return self._send(200, {"ok": True, "id": tid})
            if op == "del":
                tid = body.get("id")
                if tid in board["tasks"]:
                    for proof in board["tasks"][tid].get("proofs", []):
                        url = proof.get("url", "")
                        if url.startswith("/todo/proof-file/"):
                            fp = os.path.join(PROOFS_DIR, os.path.basename(url))
                            try:
                                os.remove(fp)
                            except OSError:
                                pass
                    del board["tasks"][tid]
                    board["order"] = [x for x in board["order"] if x != tid]
                    save_board(board, changed=[], deleted=[tid])
                    return self._send(200, {"ok": True})
                return self._send(200, {"ok": False, "error": "no_task"})
            if op == "set":
                tid = body.get("id")
                t = task_for_edit(board, tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                if "assignee" in body:
                    return self._send(400, {"ok": False, "error": "assignee_controlled"})
                prev_state = t.get("state")
                if "state" in body:
                    st = body["state"]
                    if st not in VALID_STATES:
                        return self._send(400, {"ok": False, "error": "bad_state"})
                    # Closing a card is the CEO's call alone: it kills the owner and declares the
                    # work over, so an agent must not be able to retire its own card.
                    if st in TERMINAL_STATES and actor != "CEO":
                        return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
                    t["state"] = st
                    if st == "done":
                        t["done"] = True
                for f in ("text", "doneCondition"):
                    if f in body:
                        t[f] = body[f]
                if body.get("done") is True:
                    # the same close, by another name -- gate it identically
                    if actor != "CEO":
                        return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
                    t["state"] = "done"
                    t["done"] = True
                if "verified" in body:
                    t["verified"] = bool(body["verified"])
                t["updated"] = now()
                msg = None
                if t.get("state") != prev_state:
                    apply_owner_state_transition(t, prev_state, t.get("state"), actor)
                    msg = take_task_event(t, "state -> %s" % t.get("state"))
                save_board(board, changed=[tid])
                if msg:
                    ping_boss(msg)
                return self._send(200, {"ok": True})
            if op == "pin":
                tid = body.get("id")
                t = board["tasks"].get(tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                if t.get("pinned"):
                    return self._send(200, {"ok": True})
                t = task_for_edit(board, tid)
                board["pinSeq"] += 1
                t["pinned"] = True
                t["pinRank"] = board["pinSeq"]
                save_board(board, changed=[tid])
                return self._send(200, {"ok": True})
            if op == "unpin":
                tid = body.get("id")
                t = task_for_edit(board, tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                t["pinned"] = False
                t["pinRank"] = None
                save_board(board, changed=[tid])
                return self._send(200, {"ok": True})
            return self._send(400, {"ok": False, "error": "bad_op"})

    # ---- owner ----
    def _owner(self, body, ident):
        """Boss-only. Every clause below is a way a card ends up with the wrong owner, so the
        eligibility check is deliberately exhaustive: the agent must be alive, not retired, report
        to THIS Boss, have been born for ownership, and have been born for THIS card."""
        if ident != "machine" or body.get("by") != BOSS_AGENT:
            return self._send(403, {"ok": False, "error": "boss_only"})
        action, tid, agent_id = body.get("action"), body.get("task_id"), body.get("agent_id", "")
        if action not in ("assign", "replace", "reopen") or not FULL_AGENT_ID.match(agent_id):
            return self._send(400, {"ok": False, "error": "bad_owner_request"})
        code, roster = C.http_json("GET", CFG["QUEUE_URL"] + "/roster", None, {"X-Queue-Secret": SECRET})
        if code != 200 or not isinstance(roster, list):
            return self._send(503, {"ok": False, "error": "roster_unavailable"})
        row = next((r for r in roster if r.get("agent_id") == agent_id), None)
        if (
            not row
            or row.get("state") != "alive"
            or row.get("retired") is True
            or row.get("boss_id") != BOSS_AGENT
            or row.get("lifecycle") != "owner"
            or row.get("owner_task_id") != tid
        ):
            return self._send(400, {"ok": False, "error": "ineligible_owner"})
        with LOCK:
            board = board_for_edit()
            task = task_for_edit(board, tid)
            if not task:
                return self._send(200, {"ok": False, "error": "no_task"})
            for other in board.get("tasks", {}).values():
                if (
                    other.get("id") != tid
                    and other.get("assignee") == agent_id
                    and other.get("state") not in TERMINAL_STATES
                ):
                    return self._send(409, {"ok": False, "error": "owner_busy"})
            current = task.get("assignee", "")
            if action == "assign":
                if current == agent_id:
                    return self._send(200, {"ok": True, "assignee": current, "previous": current, "action": action})
                if current:
                    return self._send(409, {"ok": False, "error": "owner_exists"})
            elif action == "replace":
                if not current:
                    return self._send(409, {"ok": False, "error": "no_owner_to_replace"})
            elif action == "reopen":
                if not task.get("ownerNeedsReplacement"):
                    return self._send(409, {"ok": False, "error": "card_not_awaiting_reopen_owner"})
                if current == agent_id:
                    return self._send(409, {"ok": False, "error": "fresh_owner_required"})
            previous = current
            task["assignee"] = agent_id
            task["ownerNeedsReplacement"] = False
            record_owner_event(task, action, agent_id, previous=previous)
            task["updated"] = now()
            save_board(board, changed=[tid])
            if action == "replace" and previous:
                request_owner_lifecycle(task, "replace", previous)
            return self._send(200, {"ok": True, "assignee": agent_id, "previous": previous, "action": action})

    # ---- comment ----
    def _comment(self, body, ident):
        with LOCK:
            board = board_for_edit()
            tid = body.get("task_id")
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            by = body.get("by", "CEO")
            to, peer = str(body.get("to") or ""), str(body.get("peer") or "")
            # peer: the a2a bridge, handing over a message signed by that paired MyPlow. It lands only on a card that
            # has already addressed that peer, so a peer cannot write itself into any card whose id it can guess.
            if peer and (ident != "machine" or str(by).split("/")[0] != peer or peer not in t.get("peers", [])):
                return self._send(200, {"ok": False, "error": "not_on_card"})
            t = task_for_edit(board, tid)
            c = {"id": uuid.uuid4().hex[:8], "by": by, "kind": "comment", "body": body.get("body", ""), "ts": now()}
            if to:
                c["to"] = to
            if peer:
                c["peer"] = peer
            elif "/" in to and to.split("/")[0] != HOST_ID:
                c["toPeer"] = to.split("/")[0]
                if c["toPeer"] not in t.get("peers", []):
                    t.setdefault("peers", []).append(c["toPeer"])
            t["comments"].append(c)
            if by != "CEO":
                t["unread"] = t.get("unread", 0) + 1
            t["updated"] = now()
            # The comment is durable before anyone is told. `to` means mp send already
            # delivered it, so this line is for the thread and it pings nobody.
            save_board(board, changed=[tid])
            if not to:
                emit_comment_event(board, t, by, c["body"])
                save_board(board, changed=[tid])
                wd_schedule_unanswered(tid, c["id"], by)
            return self._send(200, {"ok": True, "id": c["id"]})

    # ---- comment delete (one id, author or Boss only) ----
    def _comment_delete(self, body, ident):
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        tid, cid = body.get("task_id"), body.get("comment_id")
        if not tid or not cid:
            return self._send(400, {"ok": False, "error": "task_id_and_comment_id_required"})
        with LOCK:
            board = board_for_edit()
            if tid in board["tasks"]:
                task_for_edit(board, tid)
            err, c = delete_comment(board, tid, cid, actor)
            if err:
                code = 403 if err == "not_author" else 200
                return self._send(code, {"ok": False, "error": err})
            board["tasks"][tid]["updated"] = now()
            save_board(board, changed=[tid])
            try:
                with open(os.path.join(TODOS_DIR, "comment-deletes.jsonl"), "a") as f:
                    f.write(
                        json.dumps({"comment_id": cid, "card": tid, "author": c.get("by"), "actor": actor, "ts": now()})
                        + "\n"
                    )
            except OSError:
                pass
            return self._send(200, {"ok": True, "deleted": cid})

    # ---- proof (json) ----
    def _proof_json(self, body):
        with LOCK:
            board = board_for_edit()
            tid = body.get("task_id")
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            url = body.get("url", "")
            bad = unservable_proof_url(url)
            if bad:
                return self._send(200, {"ok": False, "error": "unservable_url", "detail": bad})
            t = task_for_edit(board, tid)
            kind = classify_kind(url=url, given=body.get("kind"))
            proof = {"kind": kind, "url": url, "body": body.get("body", ""), "ts": now()}
            t["proofs"].append(proof)
            t["updated"] = now()
            save_board(board, changed=[tid])
            return self._send(200, {"ok": True, "proof": proof})

    # ---- proof (multipart upload) ----
    def _proof_multipart(self):
        fields, fileobj = parse_multipart(self, self.headers.get("Content-Type", ""))
        tid = fields.get("task_id", "")
        with LOCK:
            board = board_for_edit()
            t = task_for_edit(board, tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            if fileobj:
                fn, fct, data = fileobj
                os.makedirs(PROOFS_DIR, exist_ok=True)
                safe = "%s-%s" % (uuid.uuid4().hex[:8], os.path.basename(fn))
                with open(os.path.join(PROOFS_DIR, safe), "wb") as f:
                    f.write(data)
                kind = classify_kind(filename=fn, content_type=fct, given=fields.get("kind"))
                url = "/todo/proof-file/%s" % safe
                proof = {"kind": kind, "url": url, "body": fields.get("body", ""), "ts": now()}
            else:
                url = fields.get("url", "")
                proof = {
                    "kind": classify_kind(url=url, given=fields.get("kind")),
                    "url": url,
                    "body": fields.get("body", ""),
                    "ts": now(),
                }
            t["proofs"].append(proof)
            save_board(board, changed=[tid])
            return self._send(200, {"ok": True, "proof": proof})

    # ---- status op ----
    def _status_op(self, body, ident):
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        with LOCK:
            board = board_for_edit()
            tid = body.get("task_id")
            t = task_for_edit(board, tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            st = body.get("state")
            if st and st not in VALID_STATES:
                return self._send(400, {"ok": False, "error": "bad_state"})
            if st in TERMINAL_STATES and actor != "CEO":
                return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
            prev = t.get("state")
            if st:
                t["state"] = st
                t["done"] = st == "done"
            if "verified" in body:
                t["verified"] = bool(body["verified"])
            t["updated"] = now()
            msg = None
            if st and st != prev:
                apply_owner_state_transition(t, prev, st, actor)
                msg = take_task_event(t, "state -> %s" % st)
            save_board(board, changed=[tid])
            if msg:
                ping_boss(msg)
            return self._send(200, {"ok": True})
