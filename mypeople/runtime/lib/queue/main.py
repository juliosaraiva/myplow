def main():
    threading.Thread(target=reaper, daemon=True).start()
    srv = C.BoundedThreadingHTTPServer((CFG["BIND_ADDR"], HUD_PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("queue-server on %s:%d\n" % (CFG["BIND_ADDR"], HUD_PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
