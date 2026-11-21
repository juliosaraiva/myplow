#!/usr/bin/env python3
"""First-run installer. Steps live in mypeople.host.firstrun and run inside this module.

The implementation lives in ``host/firstrun`` and is executed into this module, in order.
Imports, monkeypatches and ``__file__`` keep seeing this path.
"""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "host/firstrun"))
    namespace = globals()
    for name in (
        "preamble.py",
        "materialize.py",
        "auth.py",
        "queue_env.py",
        "hooks.py",
        "tmux.py",
        "ensure.py",
    ):
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as source:
            exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
