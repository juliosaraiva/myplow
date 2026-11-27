"""Deliver private tasks to a dedicated Codex Boss and apply its typed decisions."""

import json
import os
import runpy
import stat
import subprocess
import time
import uuid
from pathlib import Path

from store import Rejected, checked_text, digest, encode, unique_object


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["request-approval", "result"]},
        "text": {"type": "string"},
    },
    "required": ["action", "text"],
    "additionalProperties": False,
}

PRIVATE_DOCTRINE = """
## Private request role

You are handling one isolated private task. Keep its contents inside this thread and its private
return stream. Do not use tools, delegate, send notifications, or perform external actions. The
worker exclusively sends your typed decisions to this task's bound request. On the initial turn,
retain the task and choose request-approval with a short private approval question. Do not answer
the task yet. After the worker reports authenticated client approval, answer the task from this
thread and choose result. Put only the approval question or final answer in the text field.
"""


def owned(path, kind, mode=None):
    if not path.is_absolute():
        raise Rejected(400, "absolute_boss_path_required")
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or (mode is not None and stat.S_IMODE(info.st_mode) != mode):
        raise Rejected(403, "unsafe_boss_path")
    return info


def executable(path):
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise Rejected(400, "boss_executable_required")
    return path.resolve()


def boss_decision(output, request):
    if len(output) > 2 * 1024 * 1024:
        raise Rejected(502, "boss_output_invalid")
    try:
        records = [json.loads(line, object_pairs_hook=unique_object) for line in output.splitlines() if line.strip()]
        types = [record["type"] for record in records]
        threads = [record["thread_id"] for record in records if record["type"] == "thread.started"]
        items = [record["item"] for record in records if record["type"].startswith("item.")]
        messages = [record["item"]["text"] for record in records
                    if record["type"] == "item.completed" and record["item"]["type"] == "agent_message"]
        if (types.count("turn.completed") != 1 or "error" in types or "turn.failed" in types
                or len(threads) != 1 or len(messages) != 1
                or any(item["type"] not in ("reasoning", "agent_message") for item in items)):
            raise ValueError
        thread = threads[0]
        if str(uuid.UUID(thread)) != thread or (request["thread_id"] and thread != request["thread_id"]):
            raise ValueError
        decision = json.loads(messages[0], object_pairs_hook=unique_object)
        expected = "result" if request["thread_id"] else "request-approval"
        if not isinstance(decision, dict) or set(decision) != {"action", "text"} or decision["action"] != expected:
            raise ValueError
        return thread, expected, checked_text(decision["text"])
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError, Rejected):
        raise Rejected(502, "boss_output_invalid") from None


class CodexBoss:
    def __init__(self, model_home, args):
        self.codex = executable(args.codex)
        self.home = model_home
        self.codex_home = args.codex_home
        owned(self.codex_home, stat.S_ISDIR, 0o700)
        if self.codex_home.resolve() == (Path.home() / ".codex").resolve():
            raise Rejected(403, "dedicated_codex_home_required")
        try:
            auth = owned(self.codex_home / "auth.json", stat.S_ISREG, 0o600)
        except FileNotFoundError:
            raise Rejected(403, "dedicated_codex_login_required") from None
        if auth.st_nlink != 1:
            raise Rejected(403, "unsafe_boss_path")
        # Native Codex needs to persist and resume its own thread in this home.
        if (self.codex_home / "sessions").is_symlink():
            raise Rejected(403, "unsafe_boss_path")

        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "TERM": "dumb", "CODEX_HOME": str(self.codex_home)}
        for key, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_RUNTIME_DIR", "run")):
            path = self.home / directory
            path.mkdir(mode=0o700, exist_ok=True)
            owned(path, stat.S_ISDIR, 0o700)
            self.env[key] = str(path)

        self.herdr = executable(args.herdr) if args.herdr else None
        self.socket = args.herdr_socket
        if bool(self.herdr) != bool(self.socket):
            raise Rejected(400, "isolated_herdr_socket_required")
        if self.socket:
            owned(self.socket.parent, stat.S_ISDIR, 0o700)
            owned(self.socket, stat.S_ISSOCK)

        runtime = Path(__file__).resolve().parents[2]
        role_lib = runpy.run_path(str(runtime / "lib/mprole.py"))
        role = role_lib["resolve_role"]("boss", "codex", str(runtime / "roles"), "")
        self.role = role["role_ref"]
        self.role_digest = role["digest"]
        self.instructions = role_lib["_startup_doc"](role) + PRIVATE_DOCTRINE
        self.binding = digest(encode([self.role_digest, self.instructions, str(self.codex), str(self.codex_home.resolve())]))
        self.schema = self.home / "output-schema.json"
        fd = os.open(self.schema, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(encode(OUTPUT_SCHEMA))

    def turn(self, request):
        argv = [
            str(self.codex), "--no-daemon", "-a", "never", "-s", "read-only",
            "-c", 'cli_auth_credentials_store="file"', "-c", "project_doc_max_bytes=0",
            "-c", 'web_search="disabled"', "-c", "developer_instructions=" + encode(self.instructions),
            "--disable", "shell_tool", "--disable", "apps", "--disable", "plugins",
            "--disable", "hooks", "--disable", "multi_agent", "--disable", "multi_agent_v2",
            "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json",
            "--output-schema", str(self.schema),
        ]
        thread = request["thread_id"]
        if thread:
            argv += ["resume", thread]
            prompt = "The client approved the private task already in this thread. Decide the private result now."
        else:
            prompt = "Retain this private task and request approval before answering it. Task data: " + encode(request["task"])
        argv.append("-")
        env = dict(self.env)
        if self.herdr:
            env["HC_SOCKET"] = str(self.socket)
            argv = [str(self.herdr), "exec", "-i", "--timeout", "105", "--cwd", str(self.home), "--", "/usr/bin/env", "-i",
                    *(f"{key}={value}" for key, value in self.env.items()), *argv]
        try:
            proc = subprocess.run(argv, env=env, cwd=self.home, input=prompt, text=True, encoding="utf-8",
                                  capture_output=True, timeout=115)
        except subprocess.TimeoutExpired:
            raise Rejected(504, "boss_timeout") from None
        if proc.returncode:
            # Native stderr may include request or account data. Emit only a diagnostic code.
            diagnostics = (proc.stderr + proc.stdout).lower()
            if "unexpected argument" in diagnostics:
                raise Rejected(502, "boss_cli_arguments")
            if "not logged in" in diagnostics or "unauthorized" in diagnostics:
                raise Rejected(502, "boss_authentication_failed")
            raise Rejected(502, "boss_process_failed")
        return boss_decision(proc.stdout, request)


def run_worker(store, model):
    print(encode({"action": "boss_ready", "role": model.role, "role_digest": model.role_digest}), flush=True)
    while True:
        try:
            request = store.next_boss_task(model.binding)
        except Rejected as error:
            if error.reason != "route_unavailable":
                raise
            request = None
        if request is None:
            time.sleep(0.25)
            continue
        print(encode({"action": "boss_ingress", "request_id": request["request_id"], "thread_id": request["thread_id"]}), flush=True)
        thread, action, body = model.turn(request)
        try:
            event = store.boss_action(model.binding, request["request_id"], request["thread_id"], thread, action, body)
        except Rejected as error:
            if error.reason not in ("route_unavailable", "operation_forbidden", "request_expired"):
                raise
            print(encode({"action": "boss_publication_blocked", "reason": error.reason}), flush=True)
        else:
            print(encode(event), flush=True)
