# ---------- reverse proxy (stream one request to an inner port) ----------
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def proxy_request(handler, inner_host, inner_port):
    """Forward handler's current request to inner_host:inner_port, stream response back.
    Preserves Cookie / X-Queue-Secret / Set-Cookie so auth + sessions survive the proxy."""
    path = handler.path
    url = "http://%s:%s%s" % (inner_host, inner_port, path)
    if handler._enforce_body_limit(max_upload_bytes()):
        return
    length = handler._declared_length() or 0
    body = handler.rfile.read(length) if length else None
    fwd = {}
    for k, v in handler.headers.items():
        if k.lower() in HOP:
            continue
        fwd[k] = v
    req = urllib.request.Request(url, data=body, headers=fwd, method=handler.command)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        status = resp.status
        raw = resp.read()
        rhdrs = resp.getheaders()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
        rhdrs = e.headers.items()
    except Exception as e:
        handler.send_response(502)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write(("proxy error: %s" % e).encode())
        return
    if raw is None:
        raw = b""
    handler.send_response(status)
    for k, v in rhdrs:
        if k.lower() in HOP:
            continue
        handler.send_header(k, v)
    # We stripped the inner Content-Length/Connection (HOP); re-add a correct framing so
    # HTTP/1.1 keep-alive clients (curl, browser fetch) know the body is complete and don't hang.
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    if raw:
        handler.wfile.write(raw)
