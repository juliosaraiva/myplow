# Architecture

This is the map of the repository: what each directory is for, which process owns
which job, and which paths are the installed product. How the product feels to an
owner is [HOW-IT-WORKS.md](HOW-IT-WORKS.md). How to change it without breaking the
install or an upstream merge is [CONTRIBUTING.md](../CONTRIBUTING.md).

## The constraint that shapes the tree

`mypeople` (the pip package) is the installer and the supervisor. The thing that
actually runs is a **copy** of `mypeople/runtime/`, made by `firstrun.materialize()`
into `INSTALL_DIR` (default `~/.local/share/mypeople` on a native install,
`/var/lib/mypeople` in Docker).

That copy is not an importable package. Daemons start under a bare interpreter.
They put their own directory on `sys.path` and import siblings by filename
(`import mpcommon`, `import boardstore`, `import mpplugins`). Plugins do the same
against `INSTALL_DIR/bin`. `mp` is the file `INSTALL_DIR/bin/mp` on `PATH`.
HTML, JavaScript, and fonts are read from the directory of `todo-server.py`.

`materialize()` copies five trees and nothing else: `bin/`, `plugins/`, `verify/`,
`config/`, and `roles/`. A file added under one of those is shipped. A file moved
out of them is not, unless the copy list changes too.

Because of that, the source path **is** the installed path. This fork does not
relocate those trees. A shim in the old place would still have to ship, and every
upstream edit to the moved file would conflict with the move. The sections inside
the long modules are the module boundaries.

## Top level

| Path | Responsibility |
| --- | --- |
| `mypeople/` | The installable package. `mypeople.cli:main` is the `mypeople` command. `firstrun.py` materializes the runtime, checks login, and writes `queue.env`. |
| `mypeople/runtime/` | The payload that is copied onto a machine. See below. |
| `tests/` | The Python suite. Flat on purpose: `make test` loads every `test_*.py` as a top-level module, twice, in opposite orders. Index: [tests/README.md](../tests/README.md). |
| `docs/` | Product and contributor docs, plus the static site under `docs/site/`. |
| `desktop/` | The Tauri app. `desktop/scripts/bundle-runtime.sh` copies this runtime into the app bundle. |
| `cloud/` | The Plow cloud-agent image and the in-place updater (`update.py`, `publish.sh`). Separate from the self-host image. |
| `voz/` | Optional local Portuguese voice for Apple Silicon. Not part of the wheel or the Docker image. Its own README and tests. |
| `Dockerfile`, `docker-compose.yml` | The self-host image. Entrypoint is `mypeople up --both`. |
| `install.sh` | Native installer. Documented as `./install.sh`; it stays at the repo root. |
| `Makefile` | `image`, `test`, `lint`, `live`, `app`, `cloud`. |
| `pyproject.toml` | Package metadata, the `mypeople` script entry, and the ruff config. |

## What starts what

```
mypeople up
  firstrun.ensure()          copy runtime, auth, queue.env, editor hooks, tmux.conf
  supervise.sh               one of each daemon, check-before-spawn
    queue-server.py          :9900  roster, tasks, HUD
    todo-server.py           :9933  board, plugins page, access page
    queue-client.py          worker mode only: heartbeat and task relay
    board-exporter.py        git snapshot of the board, outside the working tree
    ttyd                     browser terminals
    boss-supervisor.sh       mp ensure-boss once a login exists
  plugins                    each plugins/*/plugin.conf and plugins.d/*/plugin.conf
mp                           the command agents use: spawn, send, card, comment, ...
```

`queue-server` and `todo-server` proxy each other's routes, so either port serves
the board and the HUD. Shared HTTP plumbing (body limits, sessions, the thread
cap) lives in `mpcommon.JSONHandler` and `BoundedThreadingHTTPServer`.

## `runtime/bin/`

One directory, four kinds of file. They stay together because imports, `PATH`,
and the pages all resolve next to the daemon.

### Libraries (imported, not launched)

| File | Role |
| --- | --- |
| `mpcommon.py` | Config, version, pages' shared chrome, sessions, JSON IO, notification routes, tmux delivery, HTTP and websocket clients, the shared HTTP server. |
| `boardstore.py` | SQLite board. Same load/save shape as the old JSON file. |
| `authcheck.py` | Whether this node's AI login can actually answer. Loaded by `firstrun` and by the queue client. |
| `mpplugins.py` | Which plugins exist, whether each is on, and the owner's extra text. |
| `mprole.py` | Resolve a `--role` and materialize that agent's bundle. `mp` calls it; it does not spawn. |

`mpcommon.py` is sectioned with banners (`config`, `version`, `page rendering`,
`session cookie`, `json io`, `agent ids`, `recorders`, `tmux delivery`, `HTTP`,
`websocket`, `proxy`, `HTTP server`). Read the banner, not the whole file.

### Daemons and the agent CLI

| File | Role |
| --- | --- |
| `todo-server.py` | Board HTTP API and the board, graph, plugins, and access pages. |
| `queue-server.py` | Queue, roster, reaper, HUD. |
| `queue-client.py` | Heartbeat and task relay for a worker node. |
| `board-exporter.py` | Read-only git export of the board. |
| `board-restore` | Put an exported snapshot back. The live server reloads when this writes. |
| `mp` | Agent CLI. Verbs are the `do_*` functions: spawn, send, peek, kill, answer, revive, ensure-boss, reconcile, status, board, card, comment, owner, proof, cron, tcc, plugins. |
| `supervise.sh` | Starts the daemons and the plugins. |
| `boss-supervisor.sh` | Keeps a Boss running after login. |
| `ttyd-attach.sh` | Attaches a browser terminal. |

`todo-server.py` is sectioned the same way: watchdog jobs, board store,
board-to-Boss ping, card ownership, proof classification, HTTP.

### One-shot tools

| File | Role |
| --- | --- |
| `memory-dump.py` | Board corpus for the memory skill. Reads the board; does not write it. |
| `migrate_board.py` | JSON board to SQLite and back. |
| `roster-prune.py` | Shrink `roster.json` without dropping a kill tombstone. |

### Pages

`todos.html`, `dashboard.html`, `terminal-graph.html`, `plugins.html`,
`access.html`, `textbox.js`, and `fonts/` are served by the daemon beside them.
`mpcommon.render_page` injects the version badge and the nav into those pages.

## `runtime/` besides `bin/`

| Path | Role |
| --- | --- |
| `plugins/<name>/` | A shipped plugin: a script plus `plugin.conf` (`desc`, `run`, optional `needs`). `supervise.sh` starts `run` with the folder and `serve`. |
| `roles/` | Personalities, skills, profiles, policies, hooksets, toolsets. `mp spawn --role` fails closed if this tree is missing. |
| `config/tmux.conf` | The tmux config `firstrun` installs. |
| `verify/` | The `mypeople verify` harness (`verify.sh`, browser check). |
| `mp-boss-doctrine.md` | Boss doctrine, copied to the install root (not into `bin/`). |

Installed plugins that a release did not ship live in `INSTALL_DIR/plugins.d/`.
A release deletes a shipped plugin folder it no longer contains, and never
touches `plugins.d/`.

## Config and state

`~/.config/mypeople/queue.env` (or `MYPEOPLE_CONFIG_PATH`) is the only config
file. `mpcommon.load_env()` reads it; a live environment variable overrides the
file for the keys listed there. State (board, roster, logs, plugin data) lives
under `INSTALL_DIR` and is never overwritten by `materialize()`.

Ports, from `queue.env` / the process environment: `HUD_PORT` (9900),
`TODO_PORT` (9933), `TTYD_PORT` (7681). Docker maps them with `MYPEOPLE_*_PORT`.

## Commands that must keep working

| Command | Where it is defined |
| --- | --- |
| `mypeople` | `[project.scripts]` in `pyproject.toml` → `mypeople.cli:main` |
| `mypeople up / down / status / auth-check / verify / logs` | `mypeople/cli.py` |
| `mp <verb>` | `mypeople/runtime/bin/mp` |
| `./install.sh` | repo root |
| `docker compose up` | `docker-compose.yml`, image entrypoint `mypeople up --both` |

## Upstream Plow

This repository is a fork of Plow (`delattre1/mypeople`). The runtime paths
above match that tree. Keeping them is what makes an upstream merge a normal
edit instead of a rename conflict on every daemon.

Fork-only today: `voz/`, this document, `CONTRIBUTING.md`, `tests/README.md`,
and the ruff config. The board/queue reliability work is on
`cursor/board-reliability-200a` and is not rearranged here.

Do not reformat `mypeople/runtime/` or `tests/` to land a style change. See
CONTRIBUTING.
