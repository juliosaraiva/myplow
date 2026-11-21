#!/usr/bin/env python3
"""mypeople command. The verb implementations live in mypeople.host.cli.

The implementation lives in ``host/cli`` and is executed into this module, in order.
Imports, monkeypatches and ``__file__`` keep seeing this path.
"""


def _load_implementation():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, 'host/cli'))
    namespace = globals()
    for name in (
        "preamble.py",
        "up.py",
        "down.py",
        "auth.py",
        "status.py",
        "verify.py",
        "logs.py",
    ):
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as source:
            exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
