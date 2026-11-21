# ---------- websocket client (the chat bridges: Plow's event socket, Discord's gateway) ----------
class Closed(OSError):
    """A close frame, with its code: Plow's 4401 = a refused ticket, Discord's 4004 = a dead token."""

    def __init__(self, code, reason):
        OSError.__init__(self, "websocket closed %d %s" % (code, reason))
        self.code = code


class WebSocket:
    """Just enough RFC 6455 on the stdlib: text both ways, uncompressed (no extension is offered),
    fragments joined, a ping answered, a close frame raised as Closed. `heard` is when the server
    last sent anything, so a caller can tell a dead socket from a quiet one."""

    def __init__(self, url, timeout=15):
        u = urllib.parse.urlsplit(url)
        sock = socket.create_connection((u.hostname, u.port or (80 if u.scheme == "ws" else 443)), timeout)
        if u.scheme != "ws":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock, self.buf, self.parts, self.heard = sock, b"", b"", time.time()
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET %s?%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                      % (u.path or "/", u.query, u.netloc, key)).encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if not head.startswith(b"HTTP/1.1 101") or accept not in head:
            raise OSError("websocket refused: %r" % head.splitlines()[0][:100])

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise OSError("websocket closed by the server")
        self.buf += chunk
        self.heard = time.time()

    def recv(self, timeout):
        """The next text message, or None when none came within `timeout`. Raises once closed."""
        self.sock.settimeout(timeout)
        while True:
            b = self.buf
            n, at = (b[1] & 0x7F, 2) if len(b) >= 2 else (0, 2)
            if n > 125:
                at = 4 if n == 126 else 10
                n = int.from_bytes(b[2:at], "big") if len(b) >= at else 0
            if len(b) < 2 or len(b) < at + n:      # a frame not yet whole: read more, keep what came
                try:
                    self._fill()
                except socket.timeout:
                    return None
                continue
            op, fin, data, self.buf = b[0] & 0x0F, b[0] & 0x80, b[at:at + n], b[at + n:]
            if op == 0x9:
                self.send(data, 0xA)
            elif op == 0x8:
                raise Closed(int.from_bytes(data[:2], "big") if len(data) >= 2 else 1005,
                             data[2:].decode(errors="replace"))
            elif op in (0x0, 0x1, 0x2):
                self.parts += data
                if fin:
                    text, self.parts = self.parts.decode(), b""
                    return text

    def send(self, data, op=0x1):
        """One frame, masked like every client frame: a text unless op says otherwise (ping 0x9, pong 0xA)."""
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 \
            else bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op]) + size + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

