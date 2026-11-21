#!/usr/bin/env python3
"""Entrypoint for memory-dump.py. Implementation: ../lib/board/memory_dump.py."""


def _load_implementation():
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.normpath(os.path.join(here, "../lib/board"))
    namespace = globals()
    path = os.path.join(base, "memory_dump.py")
    with open(path, encoding="utf-8") as source:
        exec(compile(source.read(), path, "exec"), namespace)


_load_implementation()
del _load_implementation
