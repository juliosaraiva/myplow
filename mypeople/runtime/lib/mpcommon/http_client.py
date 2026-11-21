# ---------- HTTP client (to central/other daemons) ----------
def http_json(method, url, body=None, headers=None, timeout=8):
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def http_upload(url, fields, file_path, headers=None, timeout=60):
    """POST multipart/form-data with one file part. Returns (status, parsed_json_or_raw).

    This is how a local artifact (screenshot, clip) becomes a board-served proof. Without it
    the only way to reference a local file is a file:// URL, which no browser will load from
    an http page.
    """
    boundary = "----mp" + base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    with open(file_path, "rb") as f:
        payload = f.read()
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, k, v)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                  'Content-Type: %s\r\n\r\n' % (boundary, os.path.basename(file_path), ctype)
                  ).encode("utf-8"))
    parts.append(payload)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    data = b"".join(parts)
    hdrs = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return 0, None

