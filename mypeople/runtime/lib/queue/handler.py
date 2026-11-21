class Handler(C.JSONHandler):
    def _auth(self):
        ok, ident = C.caller_identity(self.headers)
        return ok

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        if p == "/dashboard" or p.startswith("/dashboard/"):
            return self._send(200, raw=b"", ctype="text/html; charset=utf-8", extra=self._page_extra())
        if p in ("/", "/todos", "/terminal-graph", "/plugins") or p.startswith("/todo/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routing ----
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/health":
            return self._send(200, {"status": "ok", "uptime": int(now() - START)})
        if p == "/favicon.ico":
            return self._send(204, raw=b"")
        if p == "/dashboard" or p.startswith("/dashboard/"):
            try:
                with open(DASHBOARD_HTML, "rb") as f:
                    html = C.render_page(f.read().decode("utf-8"))
            except Exception:
                html = C.render_page("<h1>MyPlow - HUD</h1>")
            return self._send(200, raw=html, ctype="text/html; charset=utf-8", extra=self._page_extra())
        # TODO routes -> proxy to todo-server (symmetric front doors); /fonts/ is what every page links
        if p in ("/", "/todos", "/terminal-graph", "/plugins") or p.startswith("/todo/") or p.startswith("/fonts/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        # gated JSON
        if p in ("/agents", "/clients", "/roster"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            with LOCK:
                if p == "/agents":
                    # One read for the whole list, under LOCK: the roster is 700 KB, every open Graph
                    # asks every 2 s, and a read per row held the queue 95 ms each time.
                    roster = load_roster()
                    return self._send(200, [agent_row(a, r, roster) for a, r in AGENTS.items()])
                if p == "/clients":
                    return self._send(
                        200,
                        [
                            {
                                "hostname": h,
                                "attach_base": c.get("attach_base", ""),
                                "substrate_ready": c.get("substrate_ready", False),
                                "last_seen": c.get("last_seen", 0),
                                "purpose": c.get("purpose", "mypeople"),
                                "node_type": c.get("node_type", "system-agent"),
                                "recording_url": c.get("recording_url", ""),
                                "state": c.get("state", "ready"),
                            }
                            for h, c in CLIENTS.items()
                        ],
                    )
                if p == "/roster":
                    roster = load_roster()
                    out = []
                    for aid, rr in roster.items():
                        out.append(
                            {
                                "agent_id": aid,
                                "host": rr.get("host", ""),
                                "backend": rr.get("backend", "claude"),
                                "boss_id": rr.get("boss_id", ""),
                                "retired": rr.get("retired", False),
                                "retire_reason": rr.get("retire_reason", ""),
                                "retired_ts": rr.get("retired_ts"),
                                "session_id": rr.get("session_id", ""),
                                # the ownership contract: /todo/owner refuses any agent whose row does
                                # not say it was born an owner OF THAT CARD, so both must be projected
                                # here or every assignment is rejected as ineligible.
                                "lifecycle": rr.get("lifecycle", "legacy"),
                                "owner_task_id": rr.get("owner_task_id", ""),
                                "spawn_cmd": rr.get("spawn_cmd", ""),
                                "revive_cmd": "mp revive %s" % aid,
                                "cwd": rr.get("cwd", ""),
                                "model": rr.get("model", ""),
                                "summary": rr.get("summary", ""),
                                # which personality this agent was actually born with. Empty for a
                                # legacy agent spawned before the role mount existed -- consumers
                                # render that as "-" so an unmounted agent is visibly unmounted.
                                "role": rr.get("role", ""),
                                "role_ref": rr.get("role_ref", ""),
                                "role_digest": rr.get("role_digest", ""),
                                # the announced state, not just "is it in the table" -- an
                                # unauthenticated agent is present but cannot answer (8a6ebe4e48)
                                "state": (AGENTS[aid].get("state", "alive") if aid in AGENTS else "dead"),
                            }
                        )
                    return self._send(200, out)
        if p.startswith("/task/poll"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            from urllib.parse import urlparse, parse_qs

            q = parse_qs(urlparse(self.path).query)
            hn = q.get("hostname", [""])[0]
            with LOCK:
                out = []
                for tid, t in TASKS.items():
                    if t.get("delivered"):
                        continue
                    th = t.get("target_host") or ""
                    if th and th != hn:
                        continue
                    t["delivered"] = True
                    out.append(
                        {
                            "task_id": tid,
                            "type": t["type"],
                            "target_agent": t.get("target_agent", ""),
                            "payload": t.get("payload", {}),
                        }
                    )
                return self._send(200, out)
        if p.startswith("/task/"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            tid = p.split("/task/", 1)[1]
            with LOCK:
                t = TASKS.get(tid)
                if not t:
                    return self._send(404, {"error": "no_task"})
                return self._send(
                    200,
                    {
                        "task_id": tid,
                        "type": t["type"],
                        "ok": t.get("ok"),
                        "result": t.get("result"),
                        "delivered": t.get("delivered", False),
                    },
                )
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        # TODO routes -> proxy
        if p.startswith("/todo/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        if not self._auth():
            # Do not read the body. Close so those bytes are not the next request.
            if self._declared_length():
                self.close_connection = True
            return self._send(401, {"error": "unauthorized"})
        if self._enforce_body_limit(C.max_json_bytes()):
            return
        body = self._body()
        if p == "/heartbeat":
            hn = body.get("hostname", "")
            with LOCK:
                ab = body.get("attach_base", "") or ""
                from urllib.parse import urlparse

                try:
                    h = urlparse(ab).hostname if ab else ""
                except Exception:
                    h = ""
                if h in NONROUTABLE:
                    ab = ""
                c = CLIENTS.get(hn, {})
                c.update(
                    {
                        "hostname": hn,
                        "attach_base": ab,
                        "substrate_ready": body.get("substrate_ready", False),
                        "purpose": body.get("purpose", "mypeople"),
                        "node_type": body.get("node_type", "system-agent"),
                        "recording_url": body.get("recording_url", ""),
                        "state": body.get("state", "ready"),
                        "last_seen": now(),
                    }
                )
                CLIENTS[hn] = c
                upsert_agents(hn, body.get("agents", []))
            return self._send(200, {"ok": True})
        if p == "/agents/register":
            aid = body.get("agent_id")
            if not aid:
                return self._send(400, {"error": "no_agent_id"})
            with LOCK:
                upsert_agents(body.get("host", ""), [body])
            return self._send(200, {"ok": True})
        if p == "/agents/unregister":
            aid = body.get("agent_id")
            with LOCK:
                AGENTS.pop(aid, None)
            return self._send(200, {"ok": True})
        if p == "/task/submit":
            typ = body.get("type") or body.get("action")  # tolerate legacy alias on dispatch side
            if not typ:
                return self._send(400, {"error": "no_type"})
            tid = uuid.uuid4().hex[:16]
            ta = body.get("target_agent", "")
            th = C.parse_agent_id(ta)[0] if ta else ""
            with LOCK:
                TASKS[tid] = {
                    "task_id": tid,
                    "type": typ,
                    "target_agent": ta,
                    "target_host": th,
                    "payload": body.get("payload", {}),
                    "delivered": False,
                    "ok": None,
                    "result": None,
                    "ts": now(),
                }
            return self._send(200, {"task_id": tid})
        if p == "/task/result":
            tid = body.get("task_id")
            with LOCK:
                t = TASKS.get(tid)
                if t:
                    t["ok"] = body.get("ok")
                    t["result"] = body.get("result")
                    t["done_ts"] = now()
            return self._send(200, {"ok": True})
        if p == "/revive":
            aid = body.get("agent_id")
            roster = load_roster()
            if aid in roster:
                tid = uuid.uuid4().hex[:16]
                th = C.parse_agent_id(aid)[0]
                with LOCK:
                    TASKS[tid] = {
                        "task_id": tid,
                        "type": "revive",
                        "target_agent": aid,
                        "target_host": th,
                        "payload": {},
                        "delivered": False,
                        "ok": None,
                        "result": None,
                        "ts": now(),
                    }
                return self._send(200, {"ok": True, "agent_id": aid, "task_id": tid})
            return self._send(404, {"ok": False, "error": "unknown_agent"})
        return self._send(404, {"error": "not_found"})
