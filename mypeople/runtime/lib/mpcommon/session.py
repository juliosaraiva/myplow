# ---------- session cookie (stateless, HMAC-signed; NOT the secret) ----------
def _hmac(msg):
    return hmac.new(CFG.get("QUEUE_SECRET", "").encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]


def _cfg_number(name, default):
    """A config/env number. The file wins unless the live env set the key; garbage falls back."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raw = CFG.get(name, "")
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def handler_timeout():
    """Seconds a handler socket may block. None disables, matching BaseHTTPRequestHandler."""
    value = _cfg_number("MP_HANDLER_TIMEOUT", 30)
    if value <= 0:
        return None
    return value


def connection_cap():
    """Accepted connections in flight. 0 means the historical uncapped thread-per-connection."""
    value = int(_cfg_number("MP_MAX_CONNECTIONS", 128))
    return value if value > 0 else 0


def max_json_bytes():
    value = int(_cfg_number("MP_MAX_JSON_BYTES", 1024 * 1024))
    return value if value > 0 else 1024 * 1024


def max_upload_bytes():
    value = int(_cfg_number("MP_MAX_UPLOAD_BYTES", 32 * 1024 * 1024))
    return value if value > 0 else 32 * 1024 * 1024


def session_ttl():
    """Seconds a page-load session stays valid. The cookie Max-Age and the signed expiry match."""
    value = int(_cfg_number("MP_SESSION_TTL", 12 * 3600))
    return value if value > 0 else 12 * 3600


def mint_session(ttl=None, now=None):
    """nonce.expiry.hmac. The signature covers the expiry, so a client cannot extend it."""
    if ttl is None:
        ttl = session_ttl()
    if now is None:
        now = time.time()
    nonce = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    exp = int(now + ttl)
    msg = "%s.%d" % (nonce, exp)
    return "%s.%s" % (msg, _hmac(msg))


def valid_session(tok, now=None):
    if not tok or tok.count(".") != 2:
        return False
    nonce, exp, sig = tok.split(".", 2)
    mac = _hmac("%s.%s" % (nonce, exp))
    if len(sig) != len(mac) or not hmac.compare_digest(sig, mac):
        return False
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if now is None:
        now = time.time()
    return exp_i >= now


def parse_cookies(header):
    out = {}
    if not header:
        return out
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


def caller_identity(headers):
    """Returns (authorized:bool, identity:str) where identity in {machine,browser,none}."""
    secret = CFG.get("QUEUE_SECRET", "")
    hs = headers.get("X-Queue-Secret")
    if secret and hs and hmac.compare_digest(hs, secret):
        return True, "machine"
    cookies = parse_cookies(headers.get("Cookie", ""))
    if valid_session(cookies.get("mp_session", "")):
        return True, "browser"
    return False, "none"
