# ---------- HTTP server side (the board and the queue share this plumbing) ----------
class JSONHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Keep-alive responses are two writes (headers, then body). With Nagle left on, the
    # body waits for the client's delayed ACK of the headers: about 40 ms on Linux, which
    # pinned every keep-alive client near 22 requests/second.
    disable_nagle_algorithm = True

    def setup(self):
        # Per connection, so a test config and a live env both apply without a class default
        # frozen at import. None leaves the socket blocking, which is the old behavior.
        self.timeout = handler_timeout()
        super().setup()

    def log_message(self, *a):
        pass

    def _declared_length(self):
        """Content-Length as a non-negative int, or None when the header is not a length."""
        raw = self.headers.get("Content-Length")
        if raw is None or str(raw).strip() == "":
            return 0
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        if n < 0:
            return None
        return n

    def _discard(self, n):
        """Read and drop n bytes. A fixed buffer, so a rejected body does not land in RSS."""
        left = n
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)

    def _enforce_body_limit(self, limit):
        """Reject an oversized or unreadable Content-Length. True means the response was sent."""
        n = self._declared_length()
        if n is None:
            self.close_connection = True
            self._send(400, {"error": "bad_content_length"})
            return True
        if limit and n > limit:
            self._discard(n)
            self.close_connection = True
            self._send(413, {"error": "payload_too_large", "limit": limit})
            return True
        return False

    def _send(self, code, obj=None, ctype="application/json", raw=None, extra=None):
        if raw is None:
            raw = json.dumps(obj).encode("utf-8") if obj is not None else b""
        elif isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _body(self):
        n = self._declared_length()
        if not n or n > max_json_bytes():
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _page_extra(self):
        # Opening the page is the owner UI's login: the secret must not be in the HTML
        # (verify J30), and the browser then calls the API with this cookie. It expires,
        # and a token minted before expiry was signed cannot be extended by editing Max-Age.
        ttl = session_ttl()
        return {
            "Set-Cookie": "mp_session=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d" % (
                mint_session(), ttl),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses new connections once `max_connections` are in flight.

    The stock server starts a thread per accepted socket and never reaps an idle one, so a
    few hundred slow clients pinned a thread each. Excess sockets get 503 on the accept
    thread and are closed; they do not take a handler thread.
    """

    def __init__(self, server_address, RequestHandlerClass, max_connections=None):
        super().__init__(server_address, RequestHandlerClass)
        if max_connections is None:
            max_connections = connection_cap()
        self.max_connections = max_connections
        self._inflight = 0
        self._gate = threading.Lock()

    def process_request(self, request, client_address):
        if self.max_connections:
            with self._gate:
                if self._inflight >= self.max_connections:
                    self._reject(request)
                    return
                self._inflight += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def _release(self):
        if not self.max_connections:
            return
        with self._gate:
            if self._inflight:
                self._inflight -= 1

    def _reject(self, request):
        body = b'{"error":"over_cap"}'
        try:
            request.settimeout(1.0)
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n"
                b"Connection: close\r\n\r\n" % len(body) + body)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)
