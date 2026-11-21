# ---------------- HTTP ----------------
class Handler(_BoardWrite, _BoardRead, C.JSONHandler):
    """Board HTTP routes. Reads are _BoardRead, writes are _BoardWrite."""


def main():
    os.makedirs(TODOS_DIR, exist_ok=True)
    seed_board_if_missing()
    with LOCK:
        board = load_board()
        if migrate_legacy_owner_fields(board) | ensure_boss_chat(board):
            save_board(board)
    threading.Thread(target=watchdog_worker, daemon=True, name="watchdog-worker").start()
    srv = C.BoundedThreadingHTTPServer((CFG["BIND_ADDR"], TODO_PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("todo-server on %s:%d\n" % (CFG["BIND_ADDR"], TODO_PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
