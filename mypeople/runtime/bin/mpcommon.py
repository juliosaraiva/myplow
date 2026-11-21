#!/usr/bin/env python3
"""MyPlow shared helpers, loaded as the mpcommon module.

The implementation lives in ``../lib/mpcommon`` and is executed into this module, in order.
Imports, monkeypatches and ``__file__`` keep seeing this path.
"""


def _load_implementation():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, '../lib/mpcommon'))
    namespace = globals()
    for name in (
        "config.py",
        "version.py",
        "pages.py",
        "session.py",
        "jsonio.py",
        "agents.py",
        "recorders.py",
        "tmux.py",
        "http_client.py",
        "websocket.py",
        "proxy.py",
        "http_server.py",
    ):
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as source:
            exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
