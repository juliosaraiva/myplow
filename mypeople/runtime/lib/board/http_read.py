class _BoardRead:
    def _identity(self):
        return C.caller_identity(self.headers)

    def _supervisor_challenge(self):
        return self._send(
            401,
            {"error": "owner_credential_required"},
            extra={"WWW-Authenticate": 'Basic realm="MyPlow owner"', "Cache-Control": "no-store"},
        )

    def _serve_page(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                html = C.render_page(f.read())
        except Exception:
            html = C.render_page("<h1>MyPlow</h1>")
        self._send(200, raw=html, ctype="text/html; charset=utf-8", extra=self._page_extra())

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/todos") and supervisor_enabled() and not supervisor_owner_authorized(self):
            return self._supervisor_challenge()
        if p in ("/", "/todos", "/terminal-graph", "/plugins", "/access"):
            return self._send(200, raw=b"", ctype="text/html; charset=utf-8", extra=self._page_extra())
        if p == "/dashboard" or p.startswith("/dashboard/") or p in ("/agents", "/clients", "/roster"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/todo/supervisor/events":
            _, ident = self._identity()
            if not supervisor_journal_authorized(self, ident):
                return self._send(403, {"error": "supervisor_unavailable"})
            return self._send(200, {"events": supervisor_events()})
        if p == "/health":
            build = "0"
            try:
                build = str(int(os.path.getmtime(TODOS_HTML)))
            except Exception:
                pass
            return self._send(200, {"status": "ok", "build": build, "uptime": int(now() - START)})
        if p == "/favicon.ico":
            return self._send(204, raw=b"")
        # Brand fonts (bin/fonts, SIL OFL). Public like the pages that link them; basename only.
        if p == "/textbox.js":  # the text box Access and Plugins share. No login: it holds no data.
            with open(os.path.join(HTML_DIR, "textbox.js"), "rb") as f:
                return self._send(200, raw=f.read(), ctype="text/javascript; charset=utf-8")
        if p.startswith("/fonts/"):
            name = os.path.basename(p)
            fp = os.path.join(HTML_DIR, "fonts", name)
            ctype = FONT_TYPES.get(os.path.splitext(name)[1])
            if not ctype or not os.path.isfile(fp):
                return self._send(404, {"error": "no_file"})
            with open(fp, "rb") as f:
                return self._send(200, raw=f.read(), ctype=ctype, extra={"Cache-Control": "public, max-age=86400"})
        if p in ("/", "/todos"):
            if supervisor_enabled() and not supervisor_owner_authorized(self):
                return self._supervisor_challenge()
            return self._serve_page(TODOS_HTML)
        if p == "/terminal-graph":
            return self._serve_page(TERMINAL_GRAPH_HTML if os.path.exists(TERMINAL_GRAPH_HTML) else TODOS_HTML)
        if p == "/plugins":
            return self._serve_page(PLUGINS_HTML)
        if p == "/access":
            return self._serve_page(ACCESS_HTML)
        # HUD routes -> proxy to queue-server (symmetric front doors)
        if p == "/dashboard" or p.startswith("/dashboard/") or p in ("/agents", "/clients", "/roster"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        # proof file serving (both new flat + legacy nested routes)
        if p.startswith("/todo/proof-file/"):
            ok, _ = self._identity()
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            name = os.path.basename(p.split("/todo/proof-file/", 1)[1])
            fp = os.path.join(PROOFS_DIR, name)
            return self._serve_file(fp)
        if p.startswith("/todo/proof/"):
            ok, _ = self._identity()
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            rest = p.split("/todo/proof/", 1)[1].split("/")
            if len(rest) >= 2:
                tid = os.path.basename(rest[0])
                fn = os.path.basename(rest[1])
                fp = os.path.join(PROOFS_DIR, tid, fn)
                return self._serve_file(fp)
        # gated json
        ok, ident = self._identity()
        if p == "/todo/board":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            # Pre-encoded bytes, written outside LOCK. A reader that re-serialized the board
            # on every poll held the GIL long enough to stall writers; a reader that only
            # writes these bytes does not.
            return self._send(200, raw=board_json())
        if p == "/todo/terminal-graph":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, self._terminal_graph())
        if p == "/todo/plugins":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"plugins": mpplugins.listing()})
        if p == "/todo/plugins/messages":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"messages": mpplugins.messages()})
        if p == "/todo/access":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            return self._access("state", extra={"service": mpplugins.service_state("a2a")})
        if p.startswith("/todo/attach"):
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            agent = q.get("agent", [""])[0]
            code, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None, {"X-Queue-Secret": SECRET})
            base = ""
            target = C.tmux_target(agent)
            for a in agents or []:
                if a.get("agent_id") == agent:
                    base = a.get("attach_base", "")
                    target = a.get("tmux_target", target)
            return self._send(200, {"ok": True, "target": target, "base": base, "port": int(CFG["TTYD_BROWSER_PORT"])})
        return self._send(404, {"error": "not_found"})

    def _serve_file(self, fp):
        if not os.path.isfile(fp):
            return self._send(404, {"error": "no_file"})
        import mimetypes

        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            data = f.read()
        return self._send(200, raw=data, ctype=ctype)
