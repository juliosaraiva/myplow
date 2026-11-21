#!/usr/bin/env python3
"""Entrypoint for mprole.py. Implementation: ../lib/mprole.py."""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "../lib"))
    namespace = globals()
    path = os.path.join(base, "mprole.py")
    with open(path, encoding="utf-8") as source:
        exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
