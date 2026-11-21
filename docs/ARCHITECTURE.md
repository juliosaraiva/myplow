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

`materialize()` installs the six code trees `bin/`, `lib/`, `plugins/`,
`verify/`, `config/`, and `roles/`. It also copies `mp-boss-doctrine.md`,
writes `VERSION` and `run/manifest.json`, and creates the writable state
directories `todos/`, `run/`, `status/`, and `logs/` without replacing
existing board, roster, or log data.

Installed Python entrypoints stay under `bin/`. A stable launcher resolves
the active generation once, then loads its entrypoint and sibling libraries
from that immutable generation. Source loaders still execute their parts into
one module. An already-running Python launcher keeps its generation while
another installer activates a new or previous build. The loose `lib/` tree is
preserved for pre-generation code.

An interrupted installer leaves `run/runtime-install.pending` until a retry
completes. The generation pointer selects complete launcher code, and the
pending marker reports when root `VERSION` and `run/manifest.json` may still
reflect an earlier activation. Use the generation-aware installer to retry or
materialize a previous runtime with the deliberate downgrade override.

## Top level

| Path | Responsibility |
| --- | --- |
| `mypeople/cli.py`, `mypeople/firstrun.py` | Entrypoints. They load `mypeople/host/cli/` and `mypeople/host/firstrun/`. |
| `mypeople/host/` | The `mypeople` command and the installer, one file per step. |
| `mypeople/runtime/bin/` | What an install launches: loaders, shell, and the HTML/JS pages. |
| `mypeople/runtime/lib/` | The implementation those loaders execute. |
| `tests/` | `board/`, `fleet/`, `auth/`, `plugins/`, `host/`. `make test` loads every `test_*.py` twice, in opposite orders. Index: [tests/README.md](../tests/README.md). |
| `docs/` | Product and contributor docs, plus the static site under `docs/site/`. |
| `desktop/` | The Tauri app. `desktop/scripts/bundle-runtime.sh` copies this runtime into the app bundle. |
| `cloud/` | The Plow cloud-agent image and the in-place updater (`update.py`, `publish.sh`). Separate from the self-host image. |
| `voz/` | Optional local Portuguese voice for Apple Silicon. Not part of the wheel or the Docker image. Its own README and tests. |
| `Dockerfile`, `docker-compose.yml` | The self-host image. Entrypoint is `mypeople up --both`. |
| `install.sh` | Native installer. Documented as `./install.sh`; it stays at the repo root. |
| `Makefile` | `image`, `test`, `lint`, `live`, `app`, `cloud`. |
| `pyproject.toml` | Package metadata, the `mypeople` script entry, and the ruff config. |
| `.github/workflows/ci.yml` | Compiles the package, runs `ruff check`, then `make test`. |

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
mp                           agent CLI; verbs are the list under runtime/bin below
```

`queue-server` and `todo-server` proxy each other's routes, so either port serves
the board and the HUD. Shared HTTP plumbing (body limits, sessions, the thread
cap) lives in `mpcommon.JSONHandler` and `BoundedThreadingHTTPServer`.

## `runtime/lib/`

| Path | Role |
| --- | --- |
| `lib/mpcommon/` | Shared helpers, one file per section: config, version, pages, session, json IO, agents, recorders, tmux, HTTP client, websocket, proxy, HTTP server (`JSONHandler`). |
| `lib/board/` | The board server: watchdog, store, ping, ownership, proof, then `http_read.py` and `http_write.py` (the Handler mixins) and `http.py` (the class the server mounts). Also the exporter, memory dump, and migrator. |
| `lib/queue/` | Queue state and reaper, the HUD handler, `main`, the worker client, roster prune. |
| `lib/agent/` | `mp` verb groups: support, spawn, messaging, revive, fleet, status, board, cron, tcc, commands. |
| `lib/boardstore.py` | SQLite board. Same load/save shape as the old JSON file. |
| `lib/authcheck.py` | Whether this node's AI login can actually answer. |
| `lib/mpplugins.py` | Which plugins exist, whether each is on, and the owner's extra text. |
| `lib/mprole.py` | Resolve a `--role` and materialize that agent's bundle. |

## `runtime/bin/`

Loaders, shell, and pages. The loader's `__file__` is what the pages use, so
`todos.html`, `dashboard.html`, `terminal-graph.html`, `plugins.html`,
`access.html`, `textbox.js`, and `fonts/` stay beside `todo-server.py`.
`mpcommon.render_page` injects the version badge and the nav.

`mp` registers, in order: status, spawn, send, peek, kill, answer, revive,
ensure-boss, reconcile, stop-recorders, board, card, comment, proof, owner,
cron, tcc, pair, access, plugins.

`supervise.sh` still starts `bin/queue-server.py`, `bin/todo-server.py`,
`bin/queue-client.py`, `bin/board-exporter.py`, ttyd, and `boss-supervisor.sh`.
`board-restore` stays here; the live server reloads when it writes.

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

This repository is a fork of Plow (`delattre1/mypeople`). The installed
entrypoint names match that tree. The source layout does not: long modules
were split into `runtime/lib/` and `mypeople/host/`, and the tests are grouped.
An upstream edit to `mpcommon.py` or `todo-server.py` will not apply cleanly.
Port it onto the part that now holds that code, and leave the bin loader in
place so installs keep launching the same paths.

`voz/` was already fork-only. Ruff format is the house style. See CONTRIBUTING.
