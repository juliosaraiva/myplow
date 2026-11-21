"""mp model switches a live agent, Boss included, without retiring it or ending its session.

The model is a launch flag. There is no control channel into the running CLI, so the
switch respawns that same window on the new model and resumes the same session id.
Messages that arrive while the hold is open are pasted afterwards, in order, and a
resume that does not come up puts the previous launch back.
"""

import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import sandbox

BOSS = "test-node/main:Boss"
ENG = "test-node/main:eng-1"
NEW = "gpt-6.2-astra"


def boss_row(**extra):
    row = {
        "retired": False,
        "session_id": "sid-1",
        "backend": "claude",
        "is_master": True,
        "model": "luna",
        "model_pinned": True,
        "role": "boss",
        "role_ref": "boss@6.9.0",
        "role_digest": "digest-old",
        "effort": "xhigh",
        "cwd": "/work/boss",
        "session": "main",
        "tab": "Boss",
        "spawn_cmd": "mp spawn test-node/main:Boss --backend claude --cwd /work/boss --master --model luna --role boss",
        "trust": "",
    }
    row.update(extra)
    return row


class ModelSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp = sandbox.load(self.tmp.name)
        self.mp.save_roster({BOSS: boss_row()})
        self.sent = []
        self.respawns = []
        self.launches = []

    def launch(self, aid, cwd, boss_id, is_master, model, backend, resume_session=None, role_bundle=None, community=False, effort=""):
        self.launches.append(
            {
                "model": model,
                "resume_session": resume_session,
                "is_master": is_master,
                "backend": backend,
                "role_bundle": role_bundle,
                "effort": effort,
                "community": community,
            }
        )
        return "model=%s session=%s" % (model, resume_session)

    def paste(self, target, text):
        self.sent.append(text)
        return True

    def base_mocks(self, *, idle=True, ready=None, running=False, respawn_ok=True):
        mp = self.mp

        def respawn(aid, cwd, launch):
            self.respawns.append(launch)
            return respawn_ok

        def resumed(aid, backend, model, sid, timeout):
            return True if ready is None else ready(model)

        return mock.patch.multiple(
            mp,
            session_files=mock.DEFAULT,
            resolve_role=mock.DEFAULT,
            materialize_role=mock.DEFAULT,
            build_launch=self.launch,
            respawn_agent=respawn,
            wait_resumed=resumed,
            wait_idle=lambda *a, **k: idle,
            _running=lambda *a, **k: running,
            do_spawn=mock.Mock(return_value=0),
        )

    def switch(self, model=NEW, *, idle=True, ready=None, running=False, respawn_ok=True, row=None):
        """Run mp model against the Boss with the process boundary faked.

        The hold, the roster and the rollback decision are real.
        """
        if row is not None:
            self.mp.save_roster({BOSS: row})
        mp = self.mp
        out, err = io.StringIO(), io.StringIO()
        with (
            self.base_mocks(idle=idle, ready=ready, running=running, respawn_ok=respawn_ok),
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-old", "role": "boss"}),
            mock.patch.object(mp, "materialize_role", return_value={"role": "boss", "bundle": "mounted"}),
            mock.patch.object(mp.C, "tmux_send_message", side_effect=self.paste),
            mock.patch.object(mp.C, "http_json", return_value=(200, {"ok": True})) as reg,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = mp.do_model([BOSS, model])
        return rc, out.getvalue(), err.getvalue(), reg

    def test_the_boss_switches_in_place_and_keeps_its_session(self):
        queued = {"done": False}

        def respawn(aid, cwd, launch):
            self.respawns.append(launch)
            if not queued["done"]:
                self.mp.C.model_hold_prepare(aid, "one")
                self.mp.C.model_hold_prepare(aid, "two")
                self.mp.C.model_hold_prepare(aid, "two")
                queued["done"] = True
            return True

        mp = self.mp
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-old", "role": "boss"}) as role,
            mock.patch.object(mp, "materialize_role", return_value={"role": "boss", "bundle": "mounted"}),
            mock.patch.object(mp, "build_launch", side_effect=self.launch),
            mock.patch.object(mp, "respawn_agent", side_effect=respawn),
            mock.patch.object(mp, "wait_resumed", return_value=True),
            mock.patch.object(mp, "wait_idle", return_value=True),
            mock.patch.object(mp, "_running", return_value=False),
            mock.patch.object(mp, "do_spawn", return_value=0) as spawn,
            mock.patch.object(mp.C, "send_when_ready", side_effect=AssertionError("onboarding")),
            mock.patch.object(mp.C, "tmux_send_message", side_effect=self.paste),
            mock.patch.object(mp.C, "http_json", return_value=(200, {"ok": True})) as reg,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = mp.do_model([BOSS, NEW])
        self.assertEqual(0, rc, err.getvalue())
        self.assertIn("session sid-1", out.getvalue())
        spawn.assert_not_called()
        role.assert_called()
        self.assertEqual("boss@6.9.0", role.call_args.kwargs["locked_role_ref"])
        self.assertEqual(NEW, self.launches[0]["model"])
        self.assertEqual("sid-1", self.launches[0]["resume_session"])
        self.assertTrue(self.launches[0]["is_master"])
        self.assertEqual({"role": "boss", "bundle": "mounted"}, self.launches[0]["role_bundle"])
        self.assertEqual("xhigh", self.launches[0]["effort"])
        self.assertEqual(["model=%s session=sid-1" % NEW], self.respawns)
        # The duplicate text is two messages, not one message delivered twice.
        self.assertEqual(["one", "two", "two"], self.sent)
        self.assertEqual([], mp.C.model_hold_pending(BOSS))
        row = mp.load_roster()[BOSS]
        self.assertEqual(NEW, row["model"])
        self.assertTrue(row["model_pinned"])
        self.assertEqual("sid-1", row["session_id"])
        self.assertFalse(row["retired"])
        self.assertTrue(row["is_master"])
        self.assertEqual("boss", row["role"])
        self.assertEqual("boss@6.9.0", row["role_ref"])
        self.assertEqual("digest-old", row["role_digest"])
        self.assertEqual("xhigh", row["effort"])
        self.assertEqual(0, row.get("model_switch_until"))
        self.assertIn("--model %s" % NEW, row["spawn_cmd"])
        self.assertNotIn("luna", row["spawn_cmd"])
        body = reg.call_args[0][2]
        self.assertEqual(NEW, body["model"])
        self.assertTrue(body["is_master"])
        self.assertEqual("alive", body["state"])

    def test_a_failed_resume_rolls_back_and_still_delivers_the_queue_once(self):
        def respawn(aid, cwd, launch):
            self.respawns.append(launch)
            if launch.startswith("model=%s" % NEW):
                self.mp.C.model_hold_prepare(aid, "one")
                self.mp.C.model_hold_prepare(aid, "two")
            return True

        mp = self.mp
        err = io.StringIO()
        with (
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-old"}),
            mock.patch.object(mp, "materialize_role", return_value={"role": "boss"}),
            mock.patch.object(mp, "build_launch", side_effect=self.launch),
            mock.patch.object(mp, "respawn_agent", side_effect=respawn),
            mock.patch.object(mp, "wait_resumed", side_effect=lambda aid, backend, model, sid, timeout: model == "luna"),
            mock.patch.object(mp, "wait_idle", return_value=True),
            mock.patch.object(mp, "_running", return_value=False),
            mock.patch.object(mp, "do_spawn") as spawn,
            mock.patch.object(mp.C, "tmux_send_message", side_effect=self.paste),
            mock.patch.object(mp.C, "http_json", return_value=(200, {"ok": True})) as reg,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            rc = mp.do_model([BOSS, NEW])
        self.assertEqual(1, rc)
        self.assertIn("restoring luna", err.getvalue())
        self.assertIn("restored", err.getvalue())
        spawn.assert_not_called()
        reg.assert_not_called()
        self.assertEqual(["model=%s session=sid-1" % NEW, "model=luna session=sid-1"], self.respawns)
        self.assertEqual(["one", "two"], self.sent)
        self.assertEqual([], mp.C.model_hold_pending(BOSS))
        row = mp.load_roster()[BOSS]
        self.assertEqual("luna", row["model"])
        self.assertTrue(row["model_pinned"])
        self.assertEqual("sid-1", row["session_id"])
        self.assertFalse(row["retired"])
        self.assertTrue(row["is_master"])
        self.assertEqual(0, row.get("model_switch_until"))

    def test_a_respawn_that_does_not_kill_the_old_process_does_not_respawn_again(self):
        mp = self.mp
        with (
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-old"}),
            mock.patch.object(mp, "materialize_role", return_value={"role": "boss"}),
            mock.patch.object(mp, "build_launch", side_effect=self.launch),
            mock.patch.object(mp, "respawn_agent", return_value=False) as respawn,
            mock.patch.object(mp, "wait_resumed", return_value=False),
            mock.patch.object(mp, "wait_idle", return_value=True),
            mock.patch.object(mp, "_running", side_effect=lambda aid, backend, model, sid: model == "luna"),
            mock.patch.object(mp.C, "tmux_send_message", side_effect=self.paste),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(1, mp.do_model([BOSS, NEW]))
        self.assertEqual(1, respawn.call_count)
        self.assertIn(NEW, respawn.call_args[0][2])
        self.assertEqual("luna", mp.load_roster()[BOSS]["model"])
        self.assertFalse(mp.load_roster()[BOSS]["retired"])

    def test_a_paste_that_fails_after_the_switch_keeps_the_message_for_one_retry(self):
        def respawn(aid, cwd, launch):
            self.mp.C.model_hold_prepare(aid, "one")
            self.mp.C.model_hold_prepare(aid, "two")
            return True

        failed = {"two": False}

        def paste(target, text):
            if text == "two" and not failed["two"]:
                failed["two"] = True
                return False
            self.sent.append(text)
            return True

        mp = self.mp
        err = io.StringIO()
        with (
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-old"}),
            mock.patch.object(mp, "materialize_role", return_value={"role": "boss"}),
            mock.patch.object(mp, "build_launch", side_effect=self.launch),
            mock.patch.object(mp, "respawn_agent", side_effect=respawn),
            mock.patch.object(mp, "wait_resumed", return_value=True),
            mock.patch.object(mp, "wait_idle", return_value=True),
            mock.patch.object(mp, "_running", return_value=False),
            mock.patch.object(mp.C, "tmux_send_message", side_effect=paste),
            mock.patch.object(mp.C, "http_json", return_value=(200, {"ok": True})),
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, mp.do_model([BOSS, NEW]))
        self.assertEqual(["one"], self.sent)
        self.assertEqual(["two"], mp.C.model_hold_pending(BOSS))
        self.assertIn("1 message", err.getvalue())
        self.assertFalse(mp.C.model_switch_busy(BOSS))
        self.assertEqual(0, mp.C.release_model_hold(BOSS, lambda text: paste(None, text) or False))
        self.assertEqual(["one", "two"], self.sent)
        self.assertEqual([], mp.C.model_hold_pending(BOSS))
        self.assertEqual(NEW, mp.load_roster()[BOSS]["model"])

    def test_a_mid_turn_boss_is_not_touched(self):
        rc, out, err, reg = self.switch(idle=False)
        self.assertEqual(1, rc)
        self.assertIn("nothing changed", err)
        self.assertEqual([], self.respawns)
        self.assertEqual("luna", self.mp.load_roster()[BOSS]["model"])
        self.assertFalse(self.mp.load_roster()[BOSS]["retired"])
        self.assertFalse(self.mp.C.model_switch_busy(BOSS))
        reg.assert_not_called()

    def test_a_missing_transcript_refuses_before_any_respawn(self):
        mp = self.mp
        with (
            mock.patch.object(mp, "session_files", return_value=[]),
            mock.patch.object(mp, "respawn_agent") as respawn,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, mp.do_model([BOSS, NEW]))
        respawn.assert_not_called()
        self.assertEqual("luna", mp.load_roster()[BOSS]["model"])

    def test_a_retired_agent_is_sent_to_revive(self):
        self.mp.save_roster({BOSS: boss_row(retired=True)})
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(1, self.mp.do_model([BOSS, NEW]))
        self.assertIn("mp revive %s --model %s" % (BOSS, NEW), err.getvalue())

    def test_revive_still_refuses_a_live_agent(self):
        err = io.StringIO()
        with mock.patch.object(self.mp, "do_spawn") as spawn, redirect_stderr(err):
            self.assertEqual(1, self.mp.do_revive([BOSS, "--model", NEW]))
        spawn.assert_not_called()
        self.assertIn("mp model", err.getvalue())
        self.assertFalse(self.mp.load_roster()[BOSS]["retired"])

    def test_a_drifted_role_refuses_before_the_process_is_replaced(self):
        mp = self.mp
        err = io.StringIO()
        with (
            mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]),
            mock.patch.object(mp, "resolve_role", return_value={"digest": "digest-new"}),
            mock.patch.object(mp, "respawn_agent") as respawn,
            redirect_stderr(err),
        ):
            self.assertEqual(1, mp.do_model([BOSS, NEW]))
        respawn.assert_not_called()
        self.assertIn("locked role drift", err.getvalue())
        self.assertEqual("luna", mp.load_roster()[BOSS]["model"])

    def test_switching_to_the_default_model_clears_the_pin(self):
        rc, out, err, reg = self.switch(model="claude-opus-5-5")
        self.assertEqual(0, rc, err)
        row = self.mp.load_roster()[BOSS]
        self.assertEqual("claude-opus-5-5", row["model"])
        self.assertFalse(row["model_pinned"])

    def test_an_unreadable_pane_is_not_replaced(self):
        rc, out, err, reg = self.switch(running=None)
        self.assertEqual(1, rc)
        self.assertIn("could not inspect", err)
        self.assertEqual([], self.respawns)
        self.assertEqual("luna", self.mp.load_roster()[BOSS]["model"])

    def test_the_launch_line_resumes_the_same_session(self):
        text = self.mp._launch_for(BOSS, boss_row(), "claude", NEW, "sid-1", None)
        self.assertIn("--model %s" % NEW, text)
        self.assertIn("--resume sid-1", text)
        self.assertIn("--effort xhigh", text)
        self.assertIn("--dangerously-skip-permissions", text)
        codex = self.mp._launch_for(
            ENG, boss_row(is_master=False, backend="codex", effort=""), "codex", NEW, "sid-1", None
        )
        self.assertIn("--model %s" % NEW, codex)
        self.assertIn("resume sid-1", codex)
        self.assertNotIn("--resume", codex)

    def test_a_different_session_id_is_not_the_resumed_conversation(self):
        mp = self.mp
        d = Path(mp.STATUS_DIR) / "mc-main"
        d.mkdir(parents=True)
        (d / "Boss.json").write_text(json.dumps({"session_id": "brand-new"}))
        with mock.patch.object(mp.time, "sleep") as slept:
            self.assertFalse(mp.wait_resumed(BOSS, "claude", NEW, "sid-1", 30))
        slept.assert_not_called()
        (d / "Boss.json").write_text(json.dumps({"session_id": "sid-1"}))
        self.assertFalse(mp.session_diverged(BOSS, "sid-1"))

    def test_ensure_boss_does_not_retire_a_boss_mid_switch(self):
        self.mp.save_roster({BOSS: boss_row(model_switch_until=time.time() + 60)})
        with (
            mock.patch.object(self.mp.C, "tmux_windows", return_value=set()),
            mock.patch.object(self.mp, "window_exists", return_value=True),
            mock.patch.object(self.mp, "window_runs_launched_agent", return_value=False),
            mock.patch.object(self.mp, "do_revive") as revive,
            mock.patch.object(self.mp, "do_spawn") as spawn,
        ):
            self.assertEqual(0, self.mp.do_ensure_boss([BOSS, "--backend", "claude"]))
        revive.assert_not_called()
        spawn.assert_not_called()
        row = self.mp.load_roster()[BOSS]
        self.assertFalse(row["retired"])
        self.assertEqual("luna", row["model"])

    def test_reconcile_does_not_revive_an_agent_mid_switch(self):
        self.mp.save_roster(
            {
                ENG: {
                    "retired": False,
                    "is_master": False,
                    "session": "main",
                    "tab": "eng-1",
                    "backend": "claude",
                    "model": "luna",
                    "model_switch_until": time.time() + 60,
                }
            }
        )
        with (
            mock.patch.object(self.mp.C, "tmux_windows", return_value=set()),
            mock.patch.object(self.mp, "do_revive") as revive,
            mock.patch.object(self.mp, "do_spawn") as spawn,
        ):
            self.assertEqual(0, self.mp.do_reconcile([]))
        revive.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(self.mp.load_roster()[ENG].get("retired"))


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.C = sandbox.load(self.tmp.name, "mpcommon.py")
        self.aid = "%s/main:Boss" % self.C.CFG["HOST_ID"]

    def test_parked_messages_survive_a_failed_paste_and_are_not_duplicated(self):
        C = self.C
        self.assertTrue(C.model_hold_begin(self.aid, 0))
        self.assertEqual("held", C.model_hold_prepare(self.aid, "a"))
        self.assertEqual("held", C.model_hold_prepare(self.aid, "b"))
        self.assertEqual("held", C.model_hold_prepare(self.aid, "c"))
        sent = []
        failed = {"b": False}

        def send(text):
            if text == "b" and not failed["b"]:
                failed["b"] = True
                return False
            sent.append(text)
            return True

        self.assertEqual(2, C.release_model_hold(self.aid, send))
        self.assertEqual(["a"], sent)
        self.assertEqual(0, C.release_model_hold(self.aid, send))
        self.assertEqual(["a", "b", "c"], sent)
        self.assertEqual(0, C.release_model_hold(self.aid, send))
        self.assertEqual(["a", "b", "c"], sent)
        self.assertFalse(C.model_switch_busy(self.aid))

    def test_a_delivery_in_flight_keeps_the_hold_closed(self):
        C = self.C
        self.assertEqual("send", C.model_hold_prepare(self.aid, "in-flight"))
        self.assertFalse(C.model_hold_begin(self.aid, 0))
        self.assertFalse(C.model_switch_busy(self.aid))
        C.model_hold_finish_delivery(self.aid)
        self.assertTrue(C.model_hold_begin(self.aid, 0))

    def test_a_dead_switcher_does_not_trap_the_queue(self):
        C = self.C
        self.assertTrue(C.model_hold_begin(self.aid, 0))
        path, _lock = C._hold_paths(self.aid)
        st = json.loads(Path(path).read_text())
        st["switch_pid"] = 2**31 - 1
        st["held"] = ["a"]
        Path(path).write_text(json.dumps(st))
        self.assertEqual("drain", C.model_hold_prepare(self.aid, "b"))
        sent = []
        self.assertEqual(0, C.release_model_hold(self.aid, lambda text: sent.append(text) or True))
        self.assertEqual(["a", "b"], sent)

    def test_send_when_ready_parks_instead_of_pasting(self):
        C = self.C
        self.assertTrue(C.model_hold_begin(self.aid, 0))
        with mock.patch.object(C, "tmux_send_message", side_effect=AssertionError("pasted")):
            self.assertEqual("held", C.send_when_ready("mc-main:Boss", "ping", 0))
        self.assertEqual(["ping"], C.model_hold_pending(self.aid))

    def test_mp_send_accepts_a_held_message_without_cancelling_its_route(self):
        mp = sandbox.load(self.tmp.name)
        aid = "%s/main:Boss" % mp.HOST_ID
        mp.C.model_hold_begin(aid, 0)
        with (
            mock.patch.object(mp.C, "enqueue_notification_route", return_value="tok"),
            mock.patch.object(mp.C, "cancel_notification_route") as cancel,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, mp.do_send([aid, "hello", "there"]))
        cancel.assert_not_called()
        self.assertEqual(["hello there"], mp.C.model_hold_pending(aid))


class SwitchGuardTests(unittest.TestCase):
    def test_the_census_does_not_retire_an_agent_mid_switch(self):
        tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        q = sandbox.load(tmp.name, "queue-client.py")
        aid = "%s/main:Boss" % q.HOST_ID
        Path(q.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(q.ROSTER_PATH).write_text(
            json.dumps(
                {
                    aid: {
                        "retired": False,
                        "session": "main",
                        "tab": "Boss",
                        "backend": "claude",
                        "model": "luna",
                        "model_switch_until": time.time() + 60,
                    }
                }
            )
        )
        with (
            mock.patch.object(q, "auth_health", return_value={"state": "ok", "detail": ""}),
            mock.patch.object(q.C, "tmux_windows", return_value=set()),
        ):
            q.live_agents()
        row = json.loads(Path(q.ROSTER_PATH).read_text())[aid]
        self.assertFalse(row.get("retired"))
        self.assertEqual("luna", row["model"])

    def test_dispatch_asks_mp_model(self):
        tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        q = sandbox.load(tmp.name, "queue-client.py")
        with mock.patch.object(
            q.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="switched\n", stderr="")
        ) as run:
            ok, result = q.dispatch(
                {"type": "model", "target_agent": "other/main:Boss", "payload": {"model": NEW}}
            )
        self.assertTrue(ok)
        self.assertEqual(["mp", "model", "other/main:Boss", NEW], run.call_args[0][0])
        self.assertIn("switched", result)


if __name__ == "__main__":
    unittest.main()
