"""Opt-in entry point for the existing GitHub watcher service."""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sqlite3
import threading
import time

from supervisor import ProviderFailure, dispatch_one, ingest_webhook, reconcile, replay_board
from supervisor_adapters import Board, GitHub, REPO_NAME, Transport, secret
from supervisor_ledger import Ledger

SWEEP_SECONDS = 60
DISCOVERY_OVERLAP = 300
MAX_WEBHOOK = 1_048_576


def discover(ledger, provider, repos):
    for repo in repos:
        scope = f"discovery:{repo}"
        cutoff = ledger.discovery_cutoff(repo)
        started = ledger.clock()
        try:
            for row in provider.discover(repo):
                touched = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).timestamp()
                if row["state"] == "open" or touched >= cutoff:
                    ledger.remember(repo, row["number"])
        except ProviderFailure as error:
            ledger.gap(scope, error.reason)
        except (KeyError, TypeError, ValueError):
            ledger.gap(scope, "invalid_discovery_snapshot")
        else:
            ledger.discovery_completed(repo, started - DISCOVERY_OVERLAP)
            ledger.clear_gap(scope)


def handler_for(ledger, repos, key, wake):
    class Webhook(BaseHTTPRequestHandler):
        def do_POST(self):
            self.close_connection = True
            if self.path != "/github-webhook":
                self.send_error(404)
                return
            self.connection.settimeout(8)
            lengths = self.headers.get_all("Content-Length") or []
            if (
                len(lengths) != 1
                or self.headers.get("Transfer-Encoding") is not None
                or not re.fullmatch(r"[0-9]{1,9}", lengths[0])
            ):
                self.send_error(400)
                return
            size = int(lengths[0])
            if size > MAX_WEBHOOK:
                self.send_error(413)
                return
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    self.send_error(400)
                    return
                accepted = ingest_webhook(
                    ledger,
                    repos,
                    key,
                    self.headers.get("X-Hub-Signature-256", ""),
                    self.headers.get("X-GitHub-Event", ""),
                    self.headers.get("X-GitHub-Delivery", ""),
                    raw,
                )
            except sqlite3.Error:
                self.send_error(503)
                return
            except (ValueError, OSError, AttributeError, TypeError, RecursionError):
                self.send_error(400)
                return
            if accepted:
                wake.set()
            self.send_response(202)
            self.end_headers()

        def log_message(self, *_):
            pass

    return Webhook


def run(command, state_dir, cfg, boss, launcher, timeout):
    ledger = Ledger(state_dir / "supervisor.sqlite")
    if command == "status":
        print(json.dumps(ledger.status(), sort_keys=True))
        return 0
    if command not in ("serve", "once"):
        print("Use supervisor status or serve.")
        return 2
    try:
        repos = set(cfg("SUPERVISOR_REPOS").split())
        chat = cfg("SUPERVISOR_JULIO_CHAT")
        todo_port, hook_port = (
            int(cfg(key, default))
            for key, default in (
                ("TODO_PORT", "9933"),
                ("SUPERVISOR_WEBHOOK_PORT", "9937"),
            )
        )
        if (
            not repos
            or any(not REPO_NAME.fullmatch(repo) for repo in repos)
            or not re.fullmatch(r"cht_[A-Za-z0-9_-]+", chat)
            or not all(0 < port < 65536 for port in (todo_port, hook_port))
            or not cfg("QUEUE_SECRET")
        ):
            raise ValueError
        github_token_file = cfg("SUPERVISOR_GITHUB_TOKEN_FILE")
        secret(github_token_file)
        provider = GitHub(
            lambda: secret(github_token_file),
            set(cfg("SUPERVISOR_REQUIRED_CHECKS").split("|")) - {""},
            set(cfg("SUPERVISOR_APPROVERS").lower().split()),
        )
        key = secret(cfg("SUPERVISOR_WEBHOOK_KEY_FILE")).encode()
        board = Board(todo_port, cfg("QUEUE_SECRET"), secret(cfg("SUPERVISOR_JOURNAL_KEY_FILE")))
        delivery = Transport(board, chat, secret(cfg("SUPERVISOR_PLOW_TOKEN_FILE")), boss, launcher, timeout)
        request = cfg("SUPERVISOR_COPILOT_REQUEST") == "1"
    except (ValueError, ProviderFailure):
        ledger.gap("config", "invalid_or_missing_configuration")
        return 2
    ledger.clear_gap("config")
    wake = threading.Event()
    deliver = threading.Event()

    def board_once():
        try:
            if replay_board(ledger, board):
                deliver.set()
        except (ProviderFailure, KeyError, TypeError, ValueError) as error:
            ledger.gap("board", getattr(error, "reason", "invalid_journal"))
            return False
        ledger.clear_gap("board")
        return True

    def sweep():
        reconcile(ledger, provider, full=True, request_copilot=request)
        discover(ledger, provider, repos)
        reconcile(ledger, provider, request_copilot=request)
        ledger.sweep_completed()
        deliver.set()

    if command == "once":
        board_once()
        sweep()
        while dispatch_one(ledger, provider, delivery, request_copilot=request):
            pass
        return 0

    def consume_board():
        while True:
            if not board_once():
                time.sleep(5)

    def dispatch(lane):
        while True:
            deliver.clear()
            while dispatch_one(ledger, provider, delivery, request_copilot=request, lane=lane):
                pass
            deliver.wait(1)

    def worker(name, target):
        def guarded():
            while True:
                try:
                    target()
                except Exception:
                    ledger.gap(name, "worker_error")
                    time.sleep(5)
                else:
                    return

        threading.Thread(target=guarded, name=name, daemon=True).start()

    def read_provider():
        next_sweep = 0
        while True:
            wake.clear()
            at = time.monotonic()
            if at >= next_sweep:
                next_sweep = at + SWEEP_SECONDS
                sweep()
            else:
                reconcile(ledger, provider, request_copilot=request)
                deliver.set()
            wake.wait(min(1, max(0, next_sweep - time.monotonic())))

    with ThreadingHTTPServer(("127.0.0.1", hook_port), handler_for(ledger, repos, key, wake)) as server:
        worker("board_worker", consume_board)
        worker("todo_delivery_worker", lambda: dispatch("todo"))
        worker("pr_delivery_worker", lambda: dispatch("pr"))
        worker("provider_worker", read_provider)
        server.serve_forever()
    return 0
