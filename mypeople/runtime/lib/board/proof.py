# ---------------- proof kind classification ----------------
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
VID_EXT = (".mp4", ".webm", ".mov", ".m4v")


def classify_kind(url=None, filename=None, content_type=None, given=None):
    name = (filename or url or "").lower().split("?")[0]
    ct = (content_type or "").lower()
    if ct.startswith("image/") or name.endswith(IMG_EXT):
        return "image"
    if ct.startswith("video/") or name.endswith(VID_EXT):
        return "video"
    if url and (url.startswith("http://") or url.startswith("https://") or url.startswith("/")):
        return "link" if (given in (None, "", "text", "link")) else given
    if given in ("image", "video", "link"):
        return given
    return "text"


def unservable_proof_url(url):
    """Explain why a proof URL could never render on the board, or return '' if it can.

    The board is served over http, so a file:// URL or a bare local path is dead on arrival:
    the browser refuses to load it and the card shows a broken image with no explanation.
    Such a proof must be uploaded (multipart) so it is served from /todo/proof-file/.
    """
    u = (url or "").strip()
    if not u:
        return "empty url"
    if u.startswith("file://"):
        return (
            "file:// URLs cannot be loaded by the board (page is served over http). "
            "Upload the file instead: mp proof <task_id> <path> [note]"
        )
    if u.startswith("/todo/") or u.startswith("http://") or u.startswith("https://"):
        return ""
    if u.startswith("/") or u.startswith("~") or u.startswith("./") or u.startswith("../"):
        return (
            "local filesystem paths are not reachable from the board. "
            "Upload the file instead: mp proof <task_id> <path> [note]"
        )
    return ""


def parse_multipart(handler, ctype):
    """Minimal multipart/form-data parser: returns (fields:dict, file:(filename,ctype,bytes)|None)."""
    m = re.search(r'boundary=("?)([^";]+)\1', ctype)
    if not m:
        return {}, None
    boundary = ("--" + m.group(2)).encode()
    n = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(n)
    fields = {}
    fileobj = None
    for part in body.split(boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        # Strip exactly the one CRLF that delimits the part, never trailing payload bytes —
        # rstrip() would silently corrupt any file whose last bytes are 0x0d/0x0a.
        if data.endswith(b"\r\n"):
            data = data[:-2]
        htxt = head.decode("latin-1", "ignore")
        dm = re.search(r'name="([^"]+)"', htxt)
        fm = re.search(r'filename="([^"]*)"', htxt)
        cm = re.search(r"Content-Type:\s*([^\r\n]+)", htxt, re.I)
        nm = dm.group(1) if dm else "field"
        if fm and fm.group(1):
            fileobj = (fm.group(1), (cm.group(1).strip() if cm else ""), data)
        else:
            fields[nm] = data.decode("utf-8", "ignore")
    return fields, fileobj
