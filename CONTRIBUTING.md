# Contributing

Behavior and the command names stay stable. `docs/ARCHITECTURE.md` is the map
of where a change goes. This file is how to check it, and how to keep a merge
from upstream Plow possible.

## Tests

Python 3.9 or newer, stdlib only. From the repo root:

```bash
make test
```

That runs every `tests/test_*.py` in one process, then again in the reverse
order. The double pass is the point: a module that only passes when it runs
first is a failure. Plugin `*.test.mjs` files run after that when `node` is
on `PATH`.

Ten failures are already known on a Linux machine without the `claude` CLI:

- 5 need `claude` on `PATH`
- 5 are the stalled-tmux server tests in `tests/test_overload.py`

Anything else failing is a new failure. `voz/tests/` is the optional Mac voice
tool and is not part of `make test`.

## Lint

Ruff is configured in `pyproject.toml` and checks for real errors (pyflakes and
syntax), not layout.

```bash
python3 -m pip install ruff
make lint
```

`ruff format` is not applied to this tree. A format pass rewrites thousands of
lines (the runtime is hand-wrapped, often past 100 columns, and uses `%`
formatting on purpose). That diff makes an upstream merge unreadable, so it is
not the house style. Match the file you are editing.

## Where to put a change

- A daemon, `mp`, a page, or a shared helper the daemons import: `mypeople/runtime/bin/`, same filename. They import each other as siblings.
- A shipped integration: a folder under `mypeople/runtime/plugins/` with a `plugin.conf`.
- A role, skill, or personality: `mypeople/runtime/roles/`.
- The installer, login gate, or `queue.env` writer: `mypeople/firstrun.py`.
- The `mypeople` command: `mypeople/cli.py`.
- A regression test: `tests/test_<topic>.py`, still in that directory. `make test` only sees `test_*.py` there.

Do not rename a command, an HTTP path, a `queue.env` key, or a file under
`runtime/` that something imports or executes by name. If a path really has to
move, leave a shim at the old path and update Docker, `install.sh`, the desktop
bundle script, and the tests in the same change.

## Commits

Keep a move in its own commit, and the edit that follows in another. Reviewing
a rename mixed with a rewrite is how behavior changes hide. This layout change
did not move the runtime; that was a choice, explained in ARCHITECTURE.

## Upstream

Upstream is Plow. Prefer a small edit in the file Plow already has over a new
module that Plow's next change will not see. Fork-only work (`voz/`, docs,
lint config) can be new files. Do not reformat files you are not otherwise
changing.
