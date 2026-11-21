"""Installed runtime stays launchable across materialization failure and recovery."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mypeople import firstrun


class MaterializeRecoveryTests(unittest.TestCase):
    def test_missing_new_library_keeps_legacy_launcher_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp, "install")
            bindir = install / "bin"
            bindir.mkdir(parents=True)
            (bindir / "mp").write_text("print('legacy launcher')\n", encoding="utf-8")
            (install / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            copy = shutil.copytree

            def interrupt(src, dst, *args, **kwargs):
                if Path(src).name == "lib":
                    raise OSError("synthetic interrupted library copy")
                return copy(src, dst, *args, **kwargs)

            with (
                mock.patch.object(shutil, "copytree", side_effect=interrupt),
                self.assertRaisesRegex(OSError, "interrupted"),
            ):
                firstrun.materialize(str(install))
            run = subprocess.run(
                [sys.executable, str(bindir / "mp"), "--help"],
                env=dict(os.environ, INSTALL_DIR=str(install)),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual((0, "legacy launcher"), (run.returncode, run.stdout.strip()), run.stderr)
            self.assertEqual("1.0.0", (install / "VERSION").read_text().strip())


# A subprocess exits with SIGKILL exactly at the chosen production boundary.
INSTALLER = """
import os, signal, sys, time
from pathlib import Path
from unittest.mock import patch
import mypeople
from mypeople import firstrun as f

install, source, version, boundary = sys.argv[1:]
write, replace, activate = f._write_file, f._replace_file, f._activate_generation
def checkpoint(name):
    if name == boundary:
        os.kill(os.getpid(), signal.SIGKILL)
    if name == os.getenv("TEST_INSTALL_PAUSE"):
        Path(install, "pause-ready").touch()
        until = time.monotonic() + 12
        while not Path(install, "pause-release").exists():
            if time.monotonic() > until:
                raise TimeoutError("installer release")
            time.sleep(0.01)
def write_checkpoint(dst, *args):
    phase = {str(Path(install, "VERSION")):"version",
             str(Path(install, "run", "manifest.json")):"manifest",
             str(Path(install, "roles", "registry.json")):"registry"}.get(str(dst))
    if phase:
        checkpoint(phase + ":before")
    write(dst, *args)
    if phase:
        checkpoint(phase + ":after")
def replace_checkpoint(src, dst):
    replace(src, dst)
    if str(dst) == str(Path(install, "roles", "registry.json")):
        checkpoint("registry:after")
def activate_checkpoint(root, target):
    checkpoint("activate:before")
    activate(root, target)
    checkpoint("activate:after")
if os.getenv("TEST_INSTALL_WAITER"):
    import fcntl
    real_flock = fcntl.flock
    def observed_flock(fd, operation):
        try:
            real_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            Path(install, "waiter-blocked").touch()
            real_flock(fd, operation)
    fcntl.flock = observed_flock
with patch.object(f, "runtime_dir", return_value=source), patch.object(mypeople, "__version__", version), \
     patch.object(f, "_write_file", write_checkpoint), patch.object(f, "_replace_file", replace_checkpoint), \
     patch.object(f, "_activate_generation", activate_checkpoint):
    f.materialize(install)
"""


class RuntimeGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / "install"
        self.install.mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "INSTALL_DIR": str(self.install),
            "MYPEOPLE_CONFIG_PATH": str(self.root / "queue.env"),
            "HOST_ID": "fixture",
            "QUEUE_SECRET": "fixture",
            "BIND_ADDR": "127.0.0.1",
            "MYPEOPLE_ALLOW_DOWNGRADE": "1",
        }
        self.sources = {}
        for version, label in (("8.0.0", "A"), ("8.0.1", "B")):
            source = self.root / label
            shutil.copytree(firstrun.runtime_dir(), source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            with (source / "lib/agent/support.py").open("a", encoding="utf-8") as part:
                part.write(f"""
_TEST_GENERATION = {label!r}
if os.environ.get("TEST_LAUNCH_PAUSE"):
    from pathlib import Path
    gate = Path(os.environ["TEST_LAUNCH_PAUSE"])
    (gate / "ready").touch()
    until = time.monotonic() + 12
    while not (gate / "release").exists():
        if time.monotonic() > until:
            raise TimeoutError("launcher release")
        time.sleep(0.01)
""")
            commands = source / "lib/agent/commands.py"
            code = commands.read_text()
            mark = f'if os.environ.get("TEST_LAUNCH_MARK"):\n    print("generation", _TEST_GENERATION, {label!r}, C._TEST_GENERATION)\n\n'
            commands.write_text(code.replace('if __name__ == "__main__":', mark + 'if __name__ == "__main__":'))
            with (source / "lib/mpcommon/version.py").open("a", encoding="utf-8") as part:
                part.write(f"\n_TEST_GENERATION = {label!r}\n")
            self.sources[version] = source

    def install_command(self, version, boundary=""):
        return [sys.executable, "-B", "-c", INSTALLER, str(self.install), str(self.sources[version]), version, boundary]

    def run_install(self, version, boundary=""):
        return subprocess.run(
            self.install_command(version, boundary), env=self.env, capture_output=True, text=True, timeout=15
        )

    def install_version(self, version):
        outcome = self.run_install(version)
        self.assertEqual(0, outcome.returncode, outcome.stderr)

    def launch_command(self, name="mp", *args):
        return [sys.executable, "-B", str(self.install / "bin" / name), *args]

    def mp_help(self, label):
        result = subprocess.run(
            self.launch_command("mp", "--help"),
            env={**self.env, "TEST_LAUNCH_MARK": "1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"generation {label} {label} {label}", result.stdout)

    def stamp(self, active, pending=False):
        import json

        current = (self.install / ".runtime-current").resolve(strict=True)
        self.assertEqual(active, (current / "VERSION").read_text().strip())
        marker = self.install / "run/runtime-install.pending"
        self.assertEqual(pending, marker.exists())
        if not pending:
            self.assertEqual(active, (self.install / "VERSION").read_text().strip())
            manifest = json.loads((self.install / "run/manifest.json").read_text())
            self.assertEqual(active, manifest["version"])
            self.assertEqual(current, (self.install / manifest["runtime_generation"]).resolve())

    def wait_for_path(self, path, process):
        import time

        deadline = time.monotonic() + 8
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"missing deterministic gate {path.name}, exit={process.poll()}")

    def stop_child(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def child(self, args, **env):
        proc = subprocess.Popen(
            args, env={**self.env, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.addCleanup(self.stop_child, proc)
        return proc

    def test_launched_fragment_reader_keeps_generation_across_activation_and_rollback(self):
        self.install_version("8.0.0")
        for source, target, active_version in (("A", "B", "8.0.1"), ("B", "A", "8.0.0")):
            with self.subTest(source=source, target=target):
                gate = self.root / (source + target)
                gate.mkdir()
                proc = self.child(
                    self.launch_command("mp", "--help"), TEST_LAUNCH_PAUSE=str(gate), TEST_LAUNCH_MARK="1"
                )
                self.wait_for_path(gate / "ready", proc)
                self.install_version(active_version)
                self.mp_help(target)
                self.stamp(active_version)
                (gate / "release").touch()
                output, err = proc.communicate(timeout=10)
                self.assertEqual(0, proc.returncode, err)
                self.assertIn(f"generation {source} {source} {source}", output)

    def seed_board(self, backend):
        import json
        import runpy

        (self.install / "todos").mkdir(exist_ok=True)
        board = {
            "version": 2,
            "order": ["seed"],
            "pinSeq": 0,
            "tasks": {
                "seed": {"id": "seed", "state": "working", "text": "keep", "test": True, "comments": [], "proofs": []}
            },
        }
        if backend == "json":
            path = self.install / "todos/board.v2.json"
            path.write_text(json.dumps(board))
        else:
            path = self.install / "todos/board.v2.sqlite3"
            store = runpy.run_path(str(self.sources["8.0.0"] / "lib/boardstore.py"))
            self.assertTrue(store["save_board"](str(path), board))
        return path

    def board_round_trip(self, backend, update):
        import http.client
        import json
        import socket
        import sqlite3
        import time

        def port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return str(sock.getsockname()[1])

        todo, hud = port(), port()
        while todo == hud:
            hud = port()
        env = {"TODO_PORT": todo, "HUD_PORT": hud, "MYPEOPLE_BOARD_BACKEND": backend}
        board = self.child(self.launch_command("todo-server.py"), **env)
        queue = self.child(self.launch_command("queue-server.py"), **env)

        def request(target, method, path, payload=None):
            conn = http.client.HTTPConnection("127.0.0.1", int(target), timeout=2)
            try:
                conn.request(
                    method,
                    path,
                    body=json.dumps(payload) if payload is not None else None,
                    headers={"X-Queue-Secret": "fixture", "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                self.assertEqual(200, response.status)
                return json.loads(response.read())
            finally:
                conn.close()

        try:
            for target, proc in ((todo, board), (hud, queue)):
                for _ in range(150):
                    try:
                        request(target, "GET", "/health")
                        break
                    except ConnectionRefusedError:
                        if proc.poll() is not None:
                            raise AssertionError(proc.communicate(timeout=2)[1])
                        time.sleep(0.02)
                else:
                    self.fail("installed server never opened its loopback port")
            self.assertTrue(request(hud, "POST", "/heartbeat", {"hostname": "fixture"})["ok"])
            self.assertEqual("keep", request(todo, "GET", "/todo/board")["tasks"]["seed"]["text"])
            self.assertTrue(
                request(todo, "POST", "/todo/update", {"op": "set", "id": "seed", "doneCondition": update})["ok"]
            )
            self.assertEqual(update, request(todo, "GET", "/todo/board")["tasks"]["seed"]["doneCondition"])
        finally:
            self.stop_child(board)
            self.stop_child(queue)
        if backend == "sqlite":
            with sqlite3.connect(self.install / "todos/board.v2.sqlite3") as connection:
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            connection.close()

    def test_kill_boundaries_leave_usable_runtime_and_explicit_recovery(self):
        import signal

        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend):
                self.install = self.root / backend
                self.install.mkdir()
                self.env["INSTALL_DIR"] = str(self.install)
                # Start with the previous physical bin/lib layout, without a generation pointer.
                for name in ("bin", "lib"):
                    shutil.copytree(self.sources["8.0.0"] / name, self.install / name)
                (self.install / "VERSION").write_text("8.0.0\n")
                seeded = self.seed_board(backend)
                self.board_round_trip(backend, "legacy")
                original = seeded.read_bytes()
                self.install_version("8.0.0")
                self.assertEqual(original, seeded.read_bytes())
                self.stamp("8.0.0")
                for phase in (
                    "activate:before",
                    "activate:after",
                    "version:before",
                    "version:after",
                    "manifest:before",
                    "manifest:after",
                ):
                    with self.subTest(phase=phase):
                        before = seeded.read_bytes()
                        killed = self.run_install("8.0.1", phase)
                        self.assertEqual(-signal.SIGKILL, killed.returncode, killed.stderr)
                        self.assertEqual(before, seeded.read_bytes())
                        is_old = phase == "activate:before"
                        self.stamp("8.0.0" if is_old else "8.0.1", pending=True)
                        self.mp_help("A" if is_old else "B")
                        self.board_round_trip(backend, phase)
                        self.install_version("8.0.1")
                        self.stamp("8.0.1")
                        written = seeded.read_bytes()
                        self.install_version("8.0.0")
                        self.assertEqual(written, seeded.read_bytes())
                        self.stamp("8.0.0")
                self.board_round_trip(backend, "after rollback")

    def test_custom_registry_survives_kill_at_replacement_and_retry(self):
        import json
        import signal

        self.install_version("8.0.0")
        registry = self.install / "roles/registry.json"
        profile = self.install / "roles/profiles/custom/1.0.0.json"
        profile.parent.mkdir(parents=True)
        profile.write_text("{}")
        data = json.loads(registry.read_text())
        data["roles"]["custom"] = "profiles/custom/1.0.0.json"
        registry.write_text(json.dumps(data))
        for phase in ("registry:before", "registry:after"):
            with self.subTest(phase=phase):
                killed = self.run_install("8.0.1", phase)
                self.assertEqual(-signal.SIGKILL, killed.returncode, killed.stderr)
                self.assertEqual(data["roles"]["custom"], json.loads(registry.read_text())["roles"].get("custom"))
                self.install_version("8.0.1")
                self.assertEqual(data["roles"]["custom"], json.loads(registry.read_text())["roles"].get("custom"))

    def test_new_launcher_inventory_follows_activation_and_rollback(self):
        self.install_version("8.0.0")
        name = "new-queue-tool.py"
        (self.sources["8.0.1"] / "bin" / name).write_text("print('new tool')\n")
        entry = self.install / "bin" / name
        for phase in ("activate:before", "activate:after"):
            for version, before, after in (("8.0.1", False, True), ("8.0.0", True, False)):
                with self.subTest(phase=phase, version=version):
                    proc = self.child(self.install_command(version), TEST_INSTALL_PAUSE=phase)
                    self.wait_for_path(self.install / "pause-ready", proc)
                    expected = before if phase == "activate:before" else after
                    try:
                        self.assertEqual(expected, entry.exists())
                        self.assertEqual(expected, os.access(entry, os.X_OK))
                        if expected:
                            launched = subprocess.run(
                                self.launch_command(name), env=self.env, capture_output=True, text=True, timeout=10
                            )
                            self.assertEqual(
                                (0, "new tool"), (launched.returncode, launched.stdout.strip()), launched.stderr
                            )
                    finally:
                        (self.install / "pause-release").touch()
                        _, err = proc.communicate(timeout=10)
                        self.assertEqual(0, proc.returncode, err)
                        for marker in ("pause-ready", "pause-release"):
                            (self.install / marker).unlink(missing_ok=True)
                    self.assertEqual(after, entry.exists())

    def test_second_installer_waits_and_recovers_after_kill(self):
        import signal

        self.install_version("8.0.0")
        for kill in (False, True):
            with self.subTest(kill=kill):
                first = self.child(self.install_command("8.0.1"), TEST_INSTALL_PAUSE="activate:after")
                self.wait_for_path(self.install / "pause-ready", first)
                waiter = self.child(self.install_command("8.0.0"), TEST_INSTALL_WAITER="1")
                self.wait_for_path(self.install / "waiter-blocked", waiter)
                self.assertIsNone(waiter.poll())
                self.mp_help("B")
                if kill:
                    first.kill()
                else:
                    (self.install / "pause-release").touch()
                _, err = first.communicate(timeout=10)
                self.assertEqual(-signal.SIGKILL if kill else 0, first.returncode, err)
                _, err = waiter.communicate(timeout=10)
                self.assertEqual(0, waiter.returncode, err)
                self.mp_help("A")
                self.stamp("8.0.0")
                for name in ("pause-ready", "pause-release", "waiter-blocked"):
                    (self.install / name).unlink(missing_ok=True)
