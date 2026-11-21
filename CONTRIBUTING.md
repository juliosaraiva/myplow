# Contributing

Behavior and the command names stay stable. `docs/ARCHITECTURE.md` is the map
of where a change goes. This file is how to check it, and how to keep a merge
from upstream Plow possible.

## Tests

Python 3.9 or newer, stdlib only. From the repo root:

```bash
make test
```

That runs every `tests/**/test_*.py` as a package module, in one process, then
again in the reverse order. The double pass is the point: a module that only
passes when it runs first is a failure. Plugin `*.test.mjs` files run after
that when `node` is on `PATH`. GitHub Actions runs the same `make test`, and
`ruff check` before it.

The cases that used to need a `claude` binary, and the stalled-tmux cases in
`tests/test_overload.py`, are written so they run without those. On a full
`make test` in this environment, five failures are already present on `main`
and are not from a layout change: `test_install_from_git_drops_the_history`,
the three tests in `test_make_released_guard.py`, and one subtest of
`test_prefix_w_lists_the_windows_from_every_session`. They pass when those
modules run alone. `voz/tests/` is the optional Mac voice tool and is not
part of `make test`.

## Lint

Ruff formats the tree and checks pyflakes, pycodestyle, a slice of pyupgrade,
and bugbear. `%` formatting is left as written. Parts under `runtime/lib/` and
`mypeople/host/` are executed into one module, so undefined-name warnings
inside a single part are ignored.

```bash
python3 -m pip install ruff
make lint
ruff format .
```

## Where to put a change

- A shared helper the daemons import as `mpcommon`: the matching file in `mypeople/runtime/lib/mpcommon/`. The loader is `runtime/bin/mpcommon.py`.
- Board server behavior: `mypeople/runtime/lib/board/`. Reads go in `http_read.py`, writes in `http_write.py`.
- Queue server behavior: `mypeople/runtime/lib/queue/`.
- An `mp` verb: `mypeople/runtime/lib/agent/`.
- A shipped integration: a folder under `mypeople/runtime/plugins/` with a `plugin.conf`.
- A role, skill, or personality: `mypeople/runtime/roles/`.
- The installer: `mypeople/host/firstrun/`. `mypeople/firstrun.py` only loads those parts.
- The `mypeople` command: `mypeople/host/cli/`. `mypeople/cli.py` only loads those parts.
- A regression test: `tests/<board|fleet|auth|plugins|host>/test_<topic>.py`.
- Do not add a second import path. Daemons import the name in `bin/`, and the loader executes the part into that module.

Do not rename a command, an HTTP path, a `queue.env` key, or a file under
`runtime/` that something imports or executes by name. If a path really has to
move, leave a shim at the old path and update Docker, `install.sh`, the desktop
bundle script, and the tests in the same change.

## Commits

Keep a move in its own commit, and the edit that follows in another. Reviewing
a rename mixed with a rewrite is how behavior changes hide.

## Upstream

Upstream is Plow. The installed entrypoint names match. The source files do
not: see ARCHITECTURE. A change from upstream has to be applied to the part
that now holds that code. `ruff format` is already the style of this tree.
