#!/usr/bin/env python3
"""Board server. Sections: watchdog, store, ping, ownership, proof, HTTP.

The implementation lives in ``../lib/board`` and is executed into this module, in order.
Imports, monkeypatches and ``__file__`` keep seeing this path.
"""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "../lib/board"))
    namespace = globals()
    for name in (
        "preamble.py",
        "watchdog.py",
        "store.py",
        "ping.py",
        "ownership.py",
        "supervisor_journal.py",
        "proof.py",
        "http_read.py",
        "http_write.py",
        "http.py",
    ):
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as source:
            exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
