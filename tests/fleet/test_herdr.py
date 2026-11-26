"""Transport fault fixtures. The pinned Herdr binary is verified separately."""

from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

import sandbox


class HerdrTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.endpoint = root / "sessions" / "isolated" / "herdr.sock"
        self.endpoint.parent.mkdir(parents=True)
        peer = socket.socket(socket.AF_UNIX)
        self.addCleanup(peer.close)
        peer.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        sys.path.insert(0, str(sandbox.BIN))
        self.addCleanup(sys.path.remove, str(sandbox.BIN))
        import mpherdr

        self.transport = mpherdr
        self.db = root / "pilot.sqlite"
        self.agent = "fixture/herdr-case:engineer"
        self.pilot = mpherdr.Pilot(str(self.endpoint), self.db)
        self.pane = {
            "pane_id": "w1:p1",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "terminal_id": "term_a",
            "agent_status": "idle",
        }

    def spawn(self, method, **params):
        if method == "workspace.create":
            return {"root_pane": self.pane, "workspace": {"workspace_id": "w1"}}
        if method == "pane.rename":
            self.pane["label"] = params["label"]
            return {"pane": self.pane}
        if method == "pane.get":
            return {"pane": self.pane}
        return {"type": "ok"}

    def test_ambiguous_ack_is_durable_and_never_resends(self):
        with mock.patch.object(self.pilot, "rpc", side_effect=self.spawn):
            self.pilot.spawn(self.agent, "/", "true")
        calls = []

        def lost_ack(method, **params):
            if method == "pane.send_input":
                calls.append(params)
                raise self.transport.TransportError("ack lost after acceptance")
            return self.spawn(method, **params)

        for _ in range(2):
            pilot = self.transport.Pilot(str(self.endpoint), self.db)
            with mock.patch.object(pilot, "rpc", side_effect=lost_ack):
                with self.assertRaisesRegex(self.transport.TransportError, "outcome unknown"):
                    pilot.send(self.agent, "delivery-1", "fixture")
        self.assertEqual([{"pane_id": "w1:p1", "text": "fixture", "keys": ["Enter"]}], calls)

    def test_replay_and_stale_identity_do_not_write(self):
        with mock.patch.object(self.pilot, "rpc", side_effect=self.spawn) as api:
            self.pilot.spawn(self.agent, "/", "true")
            api.reset_mock()
            self.assertEqual("sent", self.pilot.send(self.agent, "delivery-1", "fixture"))
            self.assertEqual("already_sent", self.pilot.send(self.agent, "delivery-1", "fixture"))
            with self.assertRaisesRegex(self.transport.TransportError, "different text"):
                self.pilot.send(self.agent, "delivery-1", "changed")
            self.pane["terminal_id"] = "term_replaced"
            with self.assertRaisesRegex(self.transport.TransportError, "identity changed"):
                self.pilot.send(self.agent, "delivery-2", "fixture")
            self.assertEqual(1, sum(call.args[0] == "pane.send_input" for call in api.call_args_list))
