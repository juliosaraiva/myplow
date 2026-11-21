#!/usr/bin/env python3
"""Entrypoint for migrate_board.py. Implementation: ../lib/board/migrate.py."""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "../lib/board"))
    namespace = globals()
    path = os.path.join(base, "migrate.py")
    with open(path, encoding="utf-8") as source:
        exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
