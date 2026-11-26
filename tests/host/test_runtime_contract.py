"""Behavior the folder split has to keep.

These lock the command surface, the shared helpers, and the files an install
must still be able to launch. They do not care which source file a function
lives in.
"""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

import sandbox

ROOT = Path(__file__).resolve().parents[2]
VERBS = {
    "status",
    "spawn",
    "send",
    "peek",
    "reconnect",
    "kill",
    "answer",
    "revive",
    "model",
    "ensure-boss",
    "reconcile",
    "stop-recorders",
    "board",
    "card",
    "comment",
    "proof",
    "owner",
    "cron",
    "tcc",
    "pair",
    "access",
    "plugins",
}
ENTRYPOINTS = (
    "mp",
    "todo-server.py",
    "queue-server.py",
    "queue-client.py",
    "board-exporter.py",
    "mpcommon.py",
    "boardstore.py",
    "authcheck.py",
    "mpplugins.py",
    "mprole.py",
    "supervise.sh",
    "boss-supervisor.sh",
    "board-restore",
)


def _verb_fn(verb):
    return "do_" + verb.replace("-", "_")


class CommandSurfaceTests(unittest.TestCase):
    def test_mp_registers_every_verb_as_a_callable(self):
        mod = sandbox.load(tempfile.mkdtemp(), "mp")
        self.assertEqual(VERBS, set(mod.USAGE))
        for verb in VERBS:
            self.assertTrue(callable(getattr(mod, _verb_fn(verb))), verb)

    def test_the_board_and_queue_still_expose_get_and_post(self):
        home = tempfile.mkdtemp()
        board = sandbox.load(home, "todo-server.py")
        queue = sandbox.load(home + "-q", "queue-server.py")
        for mod in (board, queue):
            self.assertTrue(callable(mod.Handler.do_GET))
            self.assertTrue(callable(mod.Handler.do_POST))
            self.assertTrue(callable(mod.main))


class SharedHelperTests(unittest.TestCase):
    def test_agent_ids_and_sessions_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "config"))
            common = sandbox.load_mpcommon_standalone(td)
        self.assertEqual(("host", "sess", "tab"), common.parse_agent_id("host/sess:tab"))
        token = common.mint_session(ttl=60, now=1_000_000)
        self.assertTrue(common.valid_session(token, now=1_000_000))
        self.assertFalse(common.valid_session(token, now=1_000_000 + 61))


class InstallLayoutTests(unittest.TestCase):
    def test_materialize_leaves_launchable_entrypoints(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from mypeople import firstrun

        with tempfile.TemporaryDirectory() as td:
            install = os.path.join(td, "install")
            firstrun.materialize(install)
            for name in ENTRYPOINTS:
                path = os.path.join(install, "bin", name)
                self.assertTrue(os.path.isfile(path), name)
            common = os.path.join(install, "bin", "mpcommon.py")
            spec = importlib.util.spec_from_file_location("installed_mpcommon", common)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(("h", "s", "t"), module.parse_agent_id("h/s:t"))
