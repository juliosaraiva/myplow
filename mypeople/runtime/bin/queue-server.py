#!/usr/bin/env python3
"""Queue server. State and reaper, then the HTTP handler, then main.

The implementation lives in ``../lib/queue`` and is executed into this module, in order.
Imports, monkeypatches and ``__file__`` keep seeing this path.
"""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "../lib/queue"))
    namespace = globals()
    for name in (
        "state.py",
        "handler.py",
        "main.py",
    ):
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as source:
            exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
