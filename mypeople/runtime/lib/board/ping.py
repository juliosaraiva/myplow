# ---------------- board->Boss ping ----------------
def mp_path():
    return shutil.which("mp") or os.path.join(INSTALL_DIR, "bin", "mp")


def mp_send(agent, message):
    mp = mp_path()
    try:
        r = subprocess.run([mp, "send", agent, message], capture_output=True, text=True, timeout=25)
        rc = r.returncode
    except Exception:
        rc = -1
        r = None
    try:
        with open(INBOX_LOG, "a") as f:
            f.write("MP_SEND -> %s rc=%s :: %s\n" % (agent, rc, message[:160]))
    except Exception:
        pass
    return rc


def ping_boss(message):
    threading.Thread(target=mp_send, args=(BOSS_AGENT, message), daemon=True).start()
