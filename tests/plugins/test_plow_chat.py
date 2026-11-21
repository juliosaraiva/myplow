"""Plow Chat plugin (card f864c568e6): durable at-least-once handoff and replay safety.

What must hold: turning it on never replays history at the Boss, each new message is
delivered once, and a failed delivery is retried instead of skipped. The bridge holds Plow's
websocket (card 5b125624f8), so the same holds across a dropped socket.
"""
import base64
import email
import hashlib
import http.server
import io
from datetime import timedelta
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import sandbox

PLUGINS = Path(__file__).resolve().parents[2] / "mypeople" / "runtime" / "plugins"


def load(name, rel, env):
    return sandbox.load_file(PLUGINS / rel, name, env)


class PlowChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pc = load("plow_chat", "plow-chat/plow-chat.py",
                       {"PLOW_CHAT_STATE_DIR": self.tmp.name, "HOST_ID": "h"})
        self.msgs = [{"uid": "m1", "direction": "inbound", "body": "old", "chat_uid": "cht_a",
                      "created_at": "1"}]
        self.pc.api = lambda method, path, body=None, token=None: (200, {"data": self.msgs})
        self.sent = []
        self.pc.send_to_agent = lambda m, agent=None: self.sent.append(m) or True
        self.creds = {"token": "t", "chat_uid": "cht_a"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_an_explicit_serve_starts_the_bridge(self):
        """A bare discord-chat.py ran a second serve beside supervise.sh's (Mac 2, 10-30); no plugin defaults to serve."""
        self.pc.load_creds = lambda: self.creds
        self.pc.bridge_loop = mock.Mock()
        with mock.patch.object(sys, "argv", ["plow-chat.py"]), self.assertRaises(SystemExit):
            self.pc.main()
        self.pc.bridge_loop.assert_not_called()

    def test_seeds_then_routes_once(self):
        self.assertEqual(self.pc.BOSS_AGENT, "h/main:Boss")
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(self.sent, [], "history must not replay at the Boss")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi boss",
                          "chat_uid": "cht_a", "created_at": "2",
                          "sender": {"display_name": "Dan"}})
        self.msgs.append({"uid": "m3", "direction": "outbound", "body": "reply",
                          "chat_uid": "cht_a", "created_at": "3"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("[plowchat] from Dan in cht_a: hi boss", self.sent[0])
        self.assertIn("reply cht_a", self.sent[0])
        # Plain text: the mark-it-seen line, the message, the instructions. No reply, mention or
        # tapback lines.
        lines = self.sent[0].split("\n")
        self.assertIn("react cht_a m2 👀", lines[0])
        self.assertTrue(lines[2].startswith("(You hold this line's Plow login"))
        self.assertEqual(len(lines), 3)

    def test_the_reply_style_file_rides_in_every_message_package_with_the_agents_name(self):
        """Daniel 10-18: the style of a reply is part of the message package, from one file anyone can edit."""
        self.assertNotIn("Reply style", self.pc.envelope("Dan", "hi", "cht_a", mid="m1"), "no file, no style line")
        (Path(self.tmp.name) / "style.md").write_text("Short WhatsApp messages, in the sender's language.\n"
                                                       "Sign once: ~{name} on its own line at the bottom of the last bubble.")
        env = self.pc.envelope("Dan", "hi", "cht_a", mid="m1", agent="Chef")
        self.assertTrue(env.endswith("(Reply style: Short WhatsApp messages, in the sender's language. "
                                     "Sign once: ~Chef on its own line at the bottom of the last bubble.)"), env)
        self.assertIn("~Boss", self.pc.envelope("Dan", "hi", "cht_a", mid="m1"), "no agent named: the Boss signs")

    def test_a_reply_carries_what_it_answers_and_who_it_mentions(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({
            "uid": "msg_2", "direction": "inbound", "body": "yes that one", "chat_uid": "cht_a",
            "created_at": "2", "sender": {"type": "member", "display_name": "Dan"},
            "mentions": [{"handle": "+15550001", "is_me": True}, {"handle": "+15550002", "is_me": False}],
            "reply_to": {"part_index": 0, "message": {
                "uid": "msg_1", "body": "Deploy\n now?", "sender": {"type": "agent", "relationship": "self"}}}})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertIn('\n(in reply to your message msg_1: "Deploy now?")', self.sent[0])
        self.assertIn("\n(mentions: you, +15550002)", self.sent[0])
        self.assertIn("react cht_a msg_2 like", self.sent[0])

    def test_a_tapback_never_wakes_the_boss_and_rides_with_the_next_text_once(self):
        dan = {"type": "member", "display_name": "Dan"}
        self.msgs[0]["reactions"] = [{"uid": "rx_old", "type": "like", "actor": dan}]
        self.pc.poll_chat(self.creds, "cht_a")  # seeding: the old tapback is history
        self.msgs.append({"uid": "msg_b", "direction": "outbound", "body": "Deploy now?",
                          "chat_uid": "cht_a", "created_at": "2",
                          "sender": {"type": "agent", "relationship": "self"},
                          "reactions": [{"uid": "rx_1", "type": "love", "actor": dan},
                                        {"uid": "rx_2", "type": "like",
                                         "actor": {"type": "agent", "relationship": "self"}}]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(self.sent, [], "a tapback is not a turn")
        self.msgs.append({"uid": "msg_c", "direction": "inbound", "body": "and?",
                          "chat_uid": "cht_a", "created_at": "3", "sender": dan})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertIn('\n(earlier tapback in cht_a, separate from the new text above: Dan loved your message '
                      'msg_b: "Deploy now?")', self.sent[0])
        # The tapback rides with a real text, so it must never read as a verdict on it (10-06: "8*5" was skipped).
        self.assertTrue(self.sent[0].split("\n")[1].startswith("[plowchat] from Dan in cht_a: and?"))
        self.assertNotIn("not a new message", self.sent[0])
        self.assertNotIn("liked", self.sent[0], "history and the Boss's own tapback stay out")
        self.msgs.append({"uid": "msg_d", "direction": "inbound", "body": "?",
                          "chat_uid": "cht_a", "created_at": "4", "sender": dan})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertNotIn("earlier tapback", self.sent[1])

    def test_the_agent_is_told_to_mark_his_message_seen_before_anything_else(self):
        """The owner wanted feedback the moment a text is seen, not after the ~30s turn: the line
        right under his message tapbacks HIS message (its uid, not ours) with the eyes."""
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "msg_his", "direction": "inbound", "body": "deploy it?",
                          "chat_uid": "cht_a", "created_at": "2", "sender": {"display_name": "Dan"}})
        self.pc.poll_chat(self.creds, "cht_a")
        # The very first line, so it is the first command an agent runs, before it starts work.
        first, second = self.sent[0].split("\n")[:2]
        self.assertTrue(first.startswith("FIRST, before reading on: PLOW_CHAT_STATE_DIR="))
        self.assertIn(" react cht_a msg_his 👀", first)
        self.assertEqual("[plowchat] from Dan in cht_a: deploy it?", second)

    def test_a_second_bridge_hands_the_boss_commands_for_its_own_account(self):
        """A second Plow account (the owner's WhatsApp number as its own identity) runs its own
        bridge with its own state dir. The reply it hands the Boss must carry that dir, or the
        reply goes out under the first account's login and never reaches the chat."""
        told = self.pc.envelope("Dan", "hi", "cht_a", mid="m9")
        self.assertIn(f"PLOW_CHAT_STATE_DIR={shlex.quote(self.tmp.name)} python3 {self.pc.SELF} reply cht_a", told)
        with mock.patch.dict(os.environ):
            os.environ.pop("PLOW_CHAT_STATE_DIR", None)
            first = load("plow_chat_first", "plow-chat/plow-chat.py", {"HOST_ID": "h"})
        self.assertIn(f"run: python3 {first.SELF} reply cht_a", first.envelope("Dan", "hi", "cht_a"))
        # A bridge on another server (the WhatsApp QR number) must hand over that server too.
        other = load("plow_chat_other", "plow-chat/plow-chat.py",
                     {"PLOW_CHAT_STATE_DIR": self.tmp.name, "PLOW_CHAT_BASE_URL": "http://127.0.0.1:18790", "HOST_ID": "h"})
        self.assertIn(f"PLOW_CHAT_STATE_DIR={shlex.quote(self.tmp.name)} PLOW_CHAT_BASE_URL=http://127.0.0.1:18790 "
                      f"python3 {other.SELF} reply cht_a", other.envelope("Dan", "hi", "cht_a"))

    def test_every_text_tells_the_agent_it_holds_the_whole_plow_api(self):
        """Willow on Mac 2 told the owner "I can't create threads" (10-15): the instructions named
        only reply and react. They must say the agent holds this line's login and the whole API."""
        told = self.pc.envelope("Dan", "start a group with Mary", "cht_a", mid="m9")
        self.assertIn("https://api.plow.co/openapi.json", told)
        self.assertIn(f"{self.pc.RUN} api POST /v1/chats", told)
        self.assertIn("never answer that you can't", told)
        self.assertLess(told.index("whole Plow API"), told.index(" reply cht_a"))   # said first

    def test_api_makes_any_call_with_this_lines_login(self):
        calls = []
        self.pc.load_creds = lambda: {"token": "t"}
        self.pc.api = lambda method, path, body=None, token=None: (
            calls.append((method, path, body, token)) or (201, {"uid": "cht_new"}))
        with mock.patch.object(sys, "argv", ["plow-chat.py", "api", "post", "/v1/chats",
                                              '{"members": ["+15550100"]}']), \
                mock.patch("builtins.print") as out:
            self.pc.main()
        self.assertEqual(calls, [("POST", "/v1/chats", {"members": ["+15550100"]}, "t")])
        self.assertIn("cht_new", out.call_args[0][0])
        self.pc.api = lambda *a, **k: (403, {"error": "no"})
        for argv in (["api", "GET", "/v1/lines"], ["api", "GET", "lines"]):   # refused / not a /v1 path
            with mock.patch.object(sys, "argv", ["plow-chat.py"] + argv), mock.patch("builtins.print"), \
                    self.assertRaises(SystemExit):
                self.pc.main()

    def test_react_sends_a_tapback_and_refuses_a_misspelt_one(self):
        calls = []
        self.pc.api = lambda method, path, body=None, token=None: calls.append((path, body)) or (200, {})
        for kind in ("love", "👀", "remove"):
            self.pc.react("cht_a", "msg_1", kind, self.creds)
        with self.assertRaises(SystemExit):
            self.pc.react("cht_a", "msg_1", "thumbsup", self.creds)
        self.assertEqual(calls, [("/v1/chats/cht_a/messages/msg_1/reactions", b) for b in (
            {"operation": "add", "type": "love"},
            {"operation": "add", "type": "custom", "custom_emoji": "👀"},
            {"operation": "remove"})])

    def test_help_and_unknown_options_never_reach_the_chat(self):
        """Two engineers' `reply --help` landed in the owner's chat as the text '--help' (10-05).
        Help prints usage and sends nothing; any other leading option is refused, on every path."""
        sent = []
        self.pc.send_message = lambda text, chat_uid="", creds=None, files=(): sent.append(text) or {}
        self.pc.react = lambda *a, **k: sent.append(a) or {}
        self.pc.bridge_loop = lambda *a: sent.append("second bridge started")
        out = []
        def run(*argv):
            with mock.patch.object(self.pc.sys, "argv", ["plow-chat.py", *argv]), \
                    mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
                try:
                    self.pc.main()
                    return "ok"
                except SystemExit as e:
                    return str(e)
        for argv in (["reply", "--help"], ["reply", "-h"], ["reply", "cht_a", "--help"],
                     ["reply", "cht_a", "--file", "a.png", "--help"], ["react", "--help"],
                     ["react", "cht_a", "msg_1", "-h"], ["serve", "--help"], ["--help"]):
            self.assertEqual("ok", run(*argv), argv)
        self.assertTrue(out and all("Usage:" in o for o in out))
        for argv in (["reply", "--verbose", "hi"], ["reply", "cht_a", "--dry-run", "hi"],
                     ["reply", "cht_a", "--file"], ["react", "cht_a", "msg_1", "--like"]):
            self.assertIn("nothing sent", run(*argv), argv)
        self.assertEqual([], sent, "no help or unknown option may send anything")
        self.assertEqual("ok", run("reply", "cht_a", "- a list line"))  # a dash-led message is text
        self.assertEqual(["- a list line"], sent)

    def test_chat_that_appears_while_running_is_routed_not_swallowed(self):
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)
        self.assertIn("[plowchat]", self.sent[0])

    def test_a_send_first_chats_first_reply_reaches_the_boss(self):
        """A chat we started (Plow's send-first POST /v1/chats) first shows only our own send.
        It got no mark, so the next pass took it for a chat from before marks and swallowed
        the reply (whatsapp lab line 2, 10-14)."""
        self.msgs[:] = [{"uid": "m0", "direction": "outbound", "body": "hello", "chat_uid": "cht_a",
                         "created_at": "1"}]
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 0)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "the reply", "chat_uid": "cht_a",
                          "created_at": "2"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)
        self.assertIn("the reply", self.sent[0])

    def test_a_chat_empty_at_startup_routes_its_first_text(self):
        self.msgs[:] = []
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "first text", "chat_uid": "cht_a",
                          "created_at": "2"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)

    def test_photo_without_text_reaches_the_boss_with_its_path(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "", "chat_uid": "cht_a",
                          "created_at": "2", "attachments": [
                              {"uid": "att_1", "filename": "menu.jpeg", "url": "/v1/x?exp=1"}]})
        with mock.patch.object(self.pc, "download", return_value="/saved/att_1-menu.jpeg") as dl:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        dl.assert_called_once()
        self.assertIn("[attached, open it: /saved/att_1-menu.jpeg]", self.sent[0])

    def test_a_failed_file_becomes_a_note_and_its_text_still_arrives(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "see the video", "chat_uid": "cht_a",
                          "created_at": "2", "attachments": [
                              {"uid": "att_1", "filename": "trip.mov", "url": None, "status": "failed"}]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertIn("see the video", self.sent[0])
        self.assertIn("\n(could not download trip.mov: file unavailable)", self.sent[0])
        self.assertNotIn("open it", self.sent[0], "nothing to open: the file never arrived")

    def stt_server(self):
        """An stt-api stand-in on 127.0.0.1 only: what it got per request (form field, filename, bytes, Host), and the server."""
        heard = []

        class STT(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                form = email.message_from_bytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n"
                                                + self.rfile.read(int(self.headers["Content-Length"])))
                part = form.get_payload()[0]
                heard.append((part.get_param("name", header="content-disposition"), part.get_filename(),
                              part.get_payload(decode=True), self.headers["Host"]))
                out = json.dumps({"text": " Hey chef,  what can I cook\n tonight? "}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        stt = http.server.HTTPServer(("127.0.0.1", 0), STT)
        threading.Thread(target=stt.serve_forever, daemon=True).start()
        self.addCleanup(stt.server_close)
        self.addCleanup(stt.shutdown)
        return heard, stt

    def test_a_voice_note_is_heard_before_routing_and_a_deaf_stt_still_delivers_the_file(self):
        """Daniel 10-20: a voice note reached the agent as a file only, and the router had nothing to classify. The STT
        server's words are what JEV routes on and what the agent reads; with no words, the file comes as before."""
        audio = Path(self.tmp.name) / "att_v-3A1B.ogg"
        audio.write_bytes(b"OggS opus bytes")
        heard, stt = self.stt_server()
        routed = []
        self.pc.pick_agent = lambda text, message, *last: routed.append(text) or "h/main:Chef"
        self.pc.download = lambda a, chat_uid="", message_uid="": str(audio)
        note = lambda uid, ts: {"uid": uid, "direction": "inbound", "body": "", "chat_uid": "cht_a", "created_at": ts,
                                "sender": {"display_name": "Dan"}, "attachments": [
                                    {"uid": "att_v", "filename": "3A1B.ogg", "content_type": "audio/ogg; codecs=opus", "url": "/x"}]}
        self.pc.poll_chat(self.creds, "cht_a")
        self.pc.STT_URL = f"http://127.0.0.1:{stt.server_port}/transcribe/fast"
        self.msgs.append(note("m2", "2"))
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(heard, [("audio", "voice.ogg", b"OggS opus bytes", f"127.0.0.1:{stt.server_port}")],
                         "stt-api's form: the audio, its format in the name")
        self.assertEqual(routed, ["[voice note, transcribed] Hey chef, what can I cook tonight?"], "JEV routes on the words")
        self.assertIn("[plowchat] from Dan in cht_a: [voice note, transcribed] Hey chef, what can I cook tonight?\n"
                      f"[attached, open it: {audio}]", self.sent[0])
        self.pc.STT_URL = "http://127.0.0.1:9/transcribe/fast"  # nothing listens there: STT down
        self.msgs.append(note("m3", "3"))
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1, "a deaf STT never loses the voice note")
        self.assertEqual(routed[-1], "", "no words: nothing to classify, it stays with whoever had the chat")
        self.assertIn(f"[plowchat] from Dan in cht_a: \n[attached, open it: {audio}]", self.sent[1])
        self.assertRegex(self.sent[1], r"\n\(voice note not transcribed \(.+\): the transcript is missing, only the audio file came\)")

    def test_the_stt_server_is_reached_over_ipv4_when_its_name_also_answers_with_ipv6(self):
        """Mac 1, 10-20: server.local answered with only its IPv6 address while a VPN exit node held the IPv6 route, and
        Daniel's first voice note died on "No route to host". The call goes to the name's IPv4 address, the name kept
        as Host (exehome's proxy routes the homes' stt.int.exe.home on it)."""
        audio = Path(self.tmp.name) / "att_v-3A1B.ogg"
        audio.write_bytes(b"OggS opus bytes")
        heard, stt = self.stt_server()
        lookup = socket.getaddrinfo

        def dns(host, port, family=0, *rest):
            if host != "stt.home.test":
                return lookup(host, port, family, *rest)
            if family == socket.AF_INET:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]  # nothing listens there

        self.pc.STT_URL = f"http://stt.home.test:{stt.server_port}/transcribe/fast"
        with mock.patch.object(socket, "getaddrinfo", dns):
            self.assertEqual(self.pc.transcribe(str(audio)), "Hey chef, what can I cook tonight?")
        self.assertEqual(heard[0][3], f"stt.home.test:{stt.server_port}", "the name rides as Host")

    def test_reply_uploads_files_then_sends_their_uids(self):
        calls = []

        def api(method, path, body=None, token=None):
            calls.append((method, path, body))
            if path.endswith("/attachments"):
                return 201, {"uid": "att_out", "upload_url": "https://up/x", "upload_headers": {"h": "v"}}
            return 200, {"uid": "msg"}
        self.pc.api = api
        with tempfile.NamedTemporaryFile(suffix=".png") as f, \
                mock.patch.object(self.pc.urllib.request, "urlopen") as put:
            f.write(b"png")
            f.flush()
            self.pc.send_message("look", "cht_a", self.creds, files=[f.name])
        self.assertEqual(calls[0][2]["content_type"], "image/png")
        self.assertEqual(calls[0][2]["size_bytes"], 3)
        self.assertEqual(put.call_args[0][0].get_method(), "PUT")
        self.assertEqual(calls[1], ("POST", "/v1/chats/cht_a/messages",
                                    {"body": "look", "attachment_uids": ["att_out"]}))

    def _two_chats(self, quiet, busy):
        self.pc.api = lambda method, path, body=None, token=None: (
            200, {"data": quiet if "/cht_a/" in path else busy})

    def test_a_quiet_chats_old_messages_never_route_again(self):
        """#40: 1000+ messages elsewhere pushed a quiet chat's ids out of one shared window, and
        its unchanged page was routed to the Boss again as new."""
        busy = [{"uid": "b%d" % i, "direction": "inbound", "body": "x", "chat_uid": "cht_b",
                 "created_at": "2026-10-14T10:00:%02d.%06dZ" % (i // 1000000, i)} for i in range(1001)]
        self._two_chats(self.msgs, busy)
        self.pc.poll_chat(self.creds, "cht_a")             # seeded: m1 is history
        self.pc.poll_chat(self.creds, "cht_b", seed=False)  # a busy chat routes 1001 messages
        self.sent.clear()
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.assertEqual([], self.sent)

    def test_upgrading_from_the_id_window_neither_replays_nor_drops(self):
        old = {"uid": "m0", "direction": "inbound", "body": "routed long ago", "chat_uid": "cht_a",
               "created_at": "0"}
        new = {"uid": "m2", "direction": "inbound", "body": "landed during the restart",
               "chat_uid": "cht_a", "created_at": "2"}
        self.msgs[:] = [old, self.msgs[0], new]
        self.pc.write_json(self.pc.STATE, {"seen_ids": ["m1"], "seeded_chats": ["cht_a"]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)   # m0 aged out: not replayed
        self.assertIn("landed during the restart", self.sent[0])
        self.pc.write_json(self.pc.STATE, {"seen_ids": ["gone"], "seeded_chats": ["cht_a"]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)   # all aged out: nothing

    def test_a_quiet_chats_old_tapbacks_never_come_back(self):
        like = {"uid": "r1", "type": "like", "actor": {"type": "member", "name": "Sam"}}
        self.msgs[0].update(direction="outbound", reactions=[like])  # Sam liked the Boss's text
        busy = [{"uid": "b%d" % i, "direction": "outbound", "body": "x", "chat_uid": "cht_b",
                 "created_at": "1", "reactions": [dict(like, uid="rb%d" % i)]} for i in range(1001)]
        self._two_chats(self.msgs, busy)
        self.pc.poll_chat(self.creds, "cht_a")
        self.pc.poll_chat(self.creds, "cht_b")
        st = self.pc.read_json(self.pc.STATE, {})
        st["pending_tapbacks"] = []
        self.pc.write_json(self.pc.STATE, st)
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual([], self.pc.read_json(self.pc.STATE, {}).get("pending_tapbacks"))

    def test_times_from_different_servers_order_by_instant(self):
        o = lambda ts: self.pc.order({"created_at": ts, "uid": "u"})
        self.assertLess(o("2026-10-14T13:09:05Z"), o("2026-10-14T13:09:05.056Z"))
        self.assertLess(o("2026-10-14T13:09:05.056Z"), o("2026-10-14T13:09:05.056001+00:00"))

    def test_the_router_sends_each_text_to_the_live_agent_jev_picks_and_anything_unsure_to_the_boss(self):
        """PLOW_CHAT_ROUTER=jev: one decision, which live agent gets the text; the last agent is only context."""
        self.assertEqual(self.pc.pick_agent("hi", {}), "h/main:Boss", "off unless asked for: the Boss")
        self.pc.ROUTER = "jev"
        self.pc.INSTALL = Path(self.tmp.name) / "install"
        roles = self.pc.INSTALL / "roles"
        (roles / "profiles" / "agent-chef").mkdir(parents=True)
        (roles / "personalities" / "agent-chef").mkdir(parents=True)
        (roles / "profiles" / "agent-chef" / "1.0.0.json").write_text(
            json.dumps({"spec": {"personalityRef": "personalities/agent-chef/1.0.0.md"}}))
        (roles / "personalities" / "agent-chef" / "1.0.0.md").write_text("# Who you are\n\nYou are Chef, a cooking assistant.\n")
        status = mock.Mock(stdout="== agents ==\nagent  state  role  status\n"
                                  "h/main:Boss       [alive ] boss@6.10.0        idle boss=\n"
                                  "h/main:Chef       [alive ] agent-chef@1.0.0   idle boss=h/main:Boss\n"
                                  "h/main:Old        [dead  ] agent-old@1.0.0    -\n"
                                  "other/main:Boss   [alive ] boss@6.10.0        idle boss=\n")
        asked, sent = [], []

        def jev(req, timeout):
            asked.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"answers": {"agent": {"choice": "Chef", "confidence": 0.9}}}).encode())

        self.pc.send_to_agent = lambda m, agent=None: sent.append(agent) or True
        with mock.patch.object(self.pc.subprocess, "run", return_value=status), \
                mock.patch.object(self.pc.urllib.request, "urlopen", jev):
            agents = self.pc.live_agents()
            self.assertEqual(set(agents), {"Boss", "Chef"}, "only this host's live agents: created = in, retired = out")
            self.assertEqual(agents["Chef"][1], "You are Chef, a cooking assistant.")
            self.pc.poll_chat(self.creds, "cht_a")
            for uid, way, body, ts in (("m2", "inbound", "what can I cook?", "2"), ("m3", "outbound", "Rice!\n~Chef", "3"),
                                       ("m4", "inbound", "and for dinner?", "4")):
                self.msgs.append({"uid": uid, "direction": way, "body": body, "chat_uid": "cht_a", "created_at": ts})
                self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(sent, ["h/main:Chef", "h/main:Chef"])
        self.assertEqual(list(asked[-1]["questions"]), ["agent"], "one decision, never a second question")
        self.assertEqual(asked[0]["state"], {"message": "what can I cook?"})
        self.assertEqual(asked[-1]["state"], {"message": "and for dinner?", "last_answered_by": "Chef", "that_answer": "Rice!\n~Chef"},
                         "who answered last = the name the latest reply starts with; what they said is context too")
        # A conversation is recent answers only: minutes count, and an answer older than the window is no context.
        at = lambda minute: f"2026-10-18T21:{minute:02d}:00Z"
        reply = {"direction": "outbound", "body": "Yep, I'm here!\n~Chef", "created_at": at(24)}
        self.assertEqual(self.pc.conversation(reply, {"created_at": at(25)}), ("Chef", 1.0, "Yep, I'm here!\n~Chef"))
        self.assertEqual(self.pc.conversation(dict(reply, created_at="2026-10-18T20:00:00Z"), {"created_at": at(25)}), ())
        # The signature Daniel picked: "~Name" on its own line at the bottom of the last bubble; earlier bubbles name no one.
        self.assertEqual(self.pc.signed_by("Pancakes, step 2: flip when bubbles form.\n~Chef"), "Chef")
        self.assertEqual(self.pc.signed_by("Ingredients: 2 eggs, 1 cup flour"), "")
        self.assertEqual(self.pc.signed_by("a ~tilde mid-line is not a signature"), "")
        self.assertIn("calls an agent by name", asked[-1]["questions"]["agent"]["instructions"])
        self.assertEqual(set(asked[-1]["questions"]["agent"]["criteria"]), {"Boss", "Chef"})
        with mock.patch.object(self.pc.subprocess, "run", return_value=status), \
                mock.patch.object(self.pc.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(self.pc.pick_agent("hello?", {}, "Chef"), "h/main:Boss", "JEV down: the Boss, never lost")
            self.assertEqual(self.pc.pick_agent("", {}, "Chef"), "h/main:Chef", "a photo stays with whoever answered last")

    def test_failed_delivery_is_retried(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.send_to_agent = lambda m, agent=None: False
        self.pc.STARTED -= 300  # a bridge that has been up a while
        with mock.patch.object(self.pc, "send_message") as warn:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), -1)  # still waiting
        warn.assert_called_once()  # the sender hears it did not land
        self.pc.send_to_agent = lambda m, agent=None: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)

    def test_a_send_that_did_not_finish_is_retried_but_never_told_as_failed(self):
        """11-08: mp send timed out at 30 s while a stalled Boss was still taking the text, and Camily was
        told in English that it had not reached the team. It had, twice."""
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "[voice note, transcribed] Eu entrei pela porta",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.STARTED -= 300  # overdue: the case that used to warn
        with mock.patch.object(self.pc, "send_message") as warn:
            self.pc.send_to_agent = lambda m, agent=None: None   # unknown: it may have reached the Boss
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), -1)
            self.pc.send_to_agent = lambda m, agent=None: False  # and a refusal after that is no news to them
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), -1)
        warn.assert_not_called()
        self.pc.send_to_agent = lambda m, agent=None: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertEqual(1, len(self.sent))

    def test_a_refusal_is_told_in_the_language_they_wrote(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "Cadê minhas coisas? Você consegue ver?",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.send_to_agent = lambda m, agent=None: False
        self.pc.STARTED -= 300
        with mock.patch.object(self.pc, "send_message") as warn:
            self.pc.poll_chat(self.creds, "cht_a")
        warn.assert_called_once_with(self.pc.NOT_YET["pt"], "cht_a")
        for text, lang in (("Onde ta o boleto que eu preciso pagar?", "pt"), ("¿Dónde está mi pedido? Gracias", "es"),
                           ("Where is the invoice?", "en"), ("No audio", "en"), ("ok", "en")):
            self.assertEqual(lang, self.pc.language(text), text)

    def test_a_text_that_lands_while_the_boss_comes_back_is_not_warned_about(self):
        """An in-place update restarts the bridge before the Boss: the text waits, nobody is told."""
        self.pc.poll_chat(self.creds, "cht_a")
        now = self.pc.datetime.now(self.pc.timezone.utc)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "codename?",
                          "chat_uid": "cht_a", "created_at": now.isoformat()})
        self.pc.send_to_agent = lambda m, agent=None: False
        with mock.patch.object(self.pc, "send_message") as warn:
            self.pc.poll_chat(self.creds, "cht_a")
            self.pc.poll_chat(self.creds, "cht_a")
            warn.assert_not_called()
            self.msgs[-1]["created_at"] = (now - timedelta(seconds=300)).isoformat()
            self.pc.poll_chat(self.creds, "cht_a")
            warn.assert_not_called()  # an overdue text, but the bridge itself just came back up
            self.pc.STARTED -= 300
            self.pc.poll_chat(self.creds, "cht_a")
            self.pc.poll_chat(self.creds, "cht_a")
        warn.assert_called_once()  # overdue: told once, not on every pass
        self.pc.send_to_agent = lambda m, agent=None: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)


    def test_the_socket_routes_at_once_and_a_reconnect_drops_and_repeats_nothing(self):
        """A ping is answered and a frame routes its chat at once; a text sent while the socket was
        down arrives by the catch-up on reconnect; a fragmented frame is joined; nothing twice."""
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        self.pc.BASE = f"http://127.0.0.1:{srv.getsockname()[1]}"
        self.pc.api = lambda m, p, body=None, token=None: (
            (201, {"ticket": "t"}) if p == "/v1/ws/ticket"
            else (200, {"data": [{"uid": "cht_a"}]}) if p == "/v1/chats" else (200, {"data": self.msgs}))
        self.pc.poll_chat(self.creds, "cht_a")  # m1 is history
        msg = lambda uid: {"uid": uid, "direction": "inbound", "body": f"text {uid}",
                           "chat_uid": "cht_a", "created_at": uid}
        frame = lambda op, data, fin=0x80: bytes([fin | op]) + (  # an event needs the 16-bit length
            bytes([len(data)]) if len(data) < 126 else bytes([126]) + len(data).to_bytes(2, "big")) + data
        event = lambda uid: json.dumps({"event_type": "message_received", "chat_id": "cht_a",
                                        "data": {"message": msg(uid)}}).encode()
        pong, caught_up = [], []

        def accept():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", req).group(1)
            ok = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade"
                      b"\r\nSec-WebSocket-Accept: " + ok + b"\r\n\r\n" + frame(0x1, b'{"type": "connected"}'))
            return c

        def server():
            c = accept()
            c.sendall(frame(0x9, b"hb"))
            pong.append(c.recv(64))       # answered: the bridge is past its catch-up, listening
            self.msgs.append(msg("m2"))
            c.sendall(frame(0x1, event("m2")))
            time.sleep(0.3)
            c.close()                     # the socket drops,
            self.msgs.append(msg("m3"))   # a text lands meanwhile and its frame is never sent;
            c = accept()
            for _ in range(50):           # the catch-up on reconnect is the only way it can arrive
                if len(self.sent) >= 2:
                    break
                time.sleep(0.1)
            caught_up.append(len(self.sent))
            self.msgs.append(msg("m4"))   # a new text's frame comes in two fragments, then m2's again
            c.sendall(frame(0x1, event("m4")[:9], 0) + frame(0x0, event("m4")[9:]) + frame(0x1, event("m2")))
            time.sleep(0.3)
            c.close()

        class Stop(Exception):
            pass
        opened, real = [], self.pc.open_socket

        def open_socket(creds):
            opened.append(1)
            if len(opened) > 2:
                raise Stop
            return real(creds)
        self.pc.open_socket = open_socket
        threading.Thread(target=server, daemon=True).start()
        bridge = threading.Thread(target=lambda: self.assertRaises(Stop, self.pc.bridge_loop, self.creds),
                                  daemon=True)
        bridge.start()
        bridge.join(20)
        self.assertFalse(bridge.is_alive())
        self.assertEqual([2], caught_up)  # m3 came by the catch-up, before any frame after it
        self.assertEqual(["text m2", "text m3", "text m4"], [re.search(r"text m\d", m).group() for m in self.sent])
        head, mask, data = pong[0][:2], pong[0][2:6], pong[0][6:]
        self.assertEqual((b"\x8a\x82", b"hb"), (head, bytes(c ^ mask[i % 4] for i, c in enumerate(data))))


    def test_only_a_message_plow_marks_as_the_owner_gets_the_task_rule(self):
        for uid, stamp, sender, told in (("msg_a", "1", {"type": "member", "display_name": "Dan", "role": "owner"}, True),
                                         ("msg_b", "2", {"type": "member", "display_name": "Sam", "role": "member"}, False),
                                         ("msg_c", "3", {"type": "member", "display_name": "Nia"}, False)):
            self.sent.clear()
            self.pc.route_message({"uid": uid, "direction": "inbound", "body": "faz isso", "chat_uid": "cht_a",
                                   "created_at": stamp, "sender": sender}, "cht_a")
            self.assertEqual(self.pc.OWNER_RULE in self.sent[0], told, sender)

    def test_a_line_that_names_its_owner_gives_the_rule_to_that_person_only(self):
        self.pc.CREDS.write_text(json.dumps({"token": "t", "owner_handle": "+5511900000001"}))
        for uid, stamp, key, told in (("msg_d", "4", "+5511900000001", True), ("msg_e", "5", "+5542900000002", False)):
            self.sent.clear()
            self.pc.route_message({"uid": uid, "direction": "inbound", "body": "faz isso", "chat_uid": "cht_b", "created_at": stamp,
                                   "sender": {"type": "member", "provider_key": key, "role": "owner"}}, "cht_b")
            self.assertEqual(self.pc.OWNER_RULE in self.sent[0], told, key)


class DurableInboxTests(unittest.TestCase):
    class InjectedCrash(BaseException):
        pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"PLOW_CHAT_STATE_DIR": self.tmp.name, "HOST_ID": "h"}
        self.pc = load("plow_chat_durable", "plow-chat/plow-chat.py", self.env)
        self.pc.pick_agent = lambda *a: self.pc.BOSS_AGENT
        self.msg = {"uid": "synthetic-m2", "direction": "inbound", "body": "synthetic request",
                    "chat_uid": "synthetic-chat", "created_at": "2026-11-21T12:00:02Z",
                    "sender": {"display_name": "synthetic"}}

    def restart(self):
        pc = load("plow_chat_durable_restart", "plow-chat/plow-chat.py", self.env)
        pc.pick_agent = lambda *a: pc.BOSS_AGENT
        return pc

    def test_fresh_schema_version_and_permissions(self):
        db = self.pc.inbox_connect()
        self.assertEqual(4, db.execute("PRAGMA user_version").fetchone()[0])
        db.close()
        self.assertEqual(0o600, self.pc.INBOX_DB.stat().st_mode & 0o777)

    def test_known_prototype_migration_preserves_rows_atomically(self):
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute("""CREATE TABLE inbound_delivery(
            chat_uid TEXT NOT NULL,message_uid TEXT NOT NULL,message_json TEXT NOT NULL DEFAULT '{}',
            order_ts TEXT NOT NULL,order_uid TEXT NOT NULL,delivery_id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL DEFAULT '',attempt_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL CHECK(state IN ('pending','dispatching','ambiguous','refused','handoff_accepted')),
            last_error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL,
            PRIMARY KEY(chat_uid,message_uid))""")
        db.execute("INSERT INTO inbound_delivery VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("c", "m", '{"uid":"m"}', "t", "m", "d", "a", 4, "refused", "err", 1, 2))
        db.execute("INSERT INTO inbound_delivery VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("c", "m2", '{"body":"legacy secret"}', "t2", "m2", "d2", "a2", 5, "handoff_accepted", "", 1, 2))
        db.commit(); db.close()
        os.chmod(self.pc.INBOX_DB, 0o600)
        db = self.pc.inbox_connect()
        self.assertEqual(4, db.execute("PRAGMA user_version").fetchone()[0])
        row = db.execute("SELECT chat_uid,message_uid,message_json,delivery_id,attempt_count,state FROM inbound_delivery").fetchone()
        self.assertEqual(("c", "m", '{"uid":"m"}', "d", 4, "refused"), row)
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery WHERE message_uid='m2'").fetchone()[0])
        db.close()

    def test_unknown_schema_rolls_back_migration(self):
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute("CREATE TABLE inbound_delivery(chat_uid TEXT, mystery BLOB)"); db.commit(); db.close()
        os.chmod(self.pc.INBOX_DB, 0o600)
        before = self.pc.INBOX_DB.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.pc.inbox_connect()
        self.assertEqual(before, self.pc.INBOX_DB.read_bytes())
        db = sqlite3.connect(self.pc.INBOX_DB)
        self.assertEqual(0, db.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(["chat_uid", "mystery"], [r[1] for r in db.execute("PRAGMA table_info(inbound_delivery)")])
        db.close()

    def test_failed_known_migration_rolls_back_rename_and_ddl(self):
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute("""CREATE TABLE inbound_delivery(
            chat_uid TEXT NOT NULL,message_uid TEXT NOT NULL,message_json TEXT NOT NULL DEFAULT '{}',
            order_ts TEXT NOT NULL,order_uid TEXT NOT NULL,delivery_id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL DEFAULT '',attempt_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL CHECK(state IN ('pending','dispatching','ambiguous','refused','handoff_accepted')),
            last_error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL,
            PRIMARY KEY(chat_uid,message_uid))""")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO inbound_delivery VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("c", "m", '{"uid":"m"}', "t", "m", "d", "", 0, "corrupt-state", "", 1, 1))
        db.commit(); db.close()
        os.chmod(self.pc.INBOX_DB, 0o600)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "integrity"):
            self.pc.inbox_connect()
        db = sqlite3.connect(self.pc.INBOX_DB)
        self.assertEqual(["inbound_delivery"], [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")])
        self.assertEqual("corrupt-state", db.execute("SELECT state FROM inbound_delivery").fetchone()[0])
        self.assertEqual(0, db.execute("PRAGMA user_version").fetchone()[0])
        db.close()

    def test_corrupt_and_newer_databases_fail_closed(self):
        self.pc.INBOX_DB.write_bytes(b"not a sqlite database")
        os.chmod(self.pc.INBOX_DB, 0o600)
        corrupt = self.pc.INBOX_DB.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError): self.pc.inbox_connect()
        self.assertEqual(corrupt, self.pc.INBOX_DB.read_bytes())
        self.pc.INBOX_DB.unlink()
        db = sqlite3.connect(self.pc.INBOX_DB); db.execute("PRAGMA user_version=99"); db.commit(); db.close()
        os.chmod(self.pc.INBOX_DB, 0o600)
        future = self.pc.INBOX_DB.read_bytes()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "newer"): self.pc.inbox_connect()
        self.assertEqual(future, self.pc.INBOX_DB.read_bytes())

    def test_concurrent_open_initializes_one_schema(self):
        barrier, errors = threading.Barrier(6), []
        def open_db():
            try:
                barrier.wait()
                db = self.pc.inbox_connect()
                self.assertEqual(4, db.execute("PRAGMA user_version").fetchone()[0]); db.close()
            except BaseException as exc: errors.append(exc)
        threads = [threading.Thread(target=open_db) for _ in range(6)]
        [t.start() for t in threads]; [t.join(5) for t in threads]
        self.assertFalse(any(t.is_alive() for t in threads)); self.assertEqual([], errors)

    def test_cross_process_open_close_stress_has_no_transient_sidecar_rejection(self):
        plugin = str(Path(__file__).resolve().parents[2] / "mypeople/runtime/plugins/plow-chat/plow-chat.py")
        code = ("import runpy,sys; n=runpy.run_path(sys.argv[1]); "
                "[(lambda d:d.close())(n['inbox_connect']()) for _ in range(60)]")
        env = dict(os.environ, PLOW_CHAT_STATE_DIR=self.tmp.name, HOST_ID="h")
        procs = [subprocess.Popen([sys.executable, "-c", code, plugin], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        results = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
        self.assertEqual([0] * 4, [result[2] for result in results], results)

    def test_shm_unlinked_inode_is_tolerated_but_other_unsafe_metadata_is_rejected(self):
        self.pc.inbox_connect().close()
        shm = Path(str(self.pc.INBOX_DB) + "-shm")
        shm.write_bytes(b"x"); os.chmod(shm, 0o600)
        original = Path.lstat
        def unlinked_lstat(path):
            st = original(path)
            if path == shm:
                # Supply fields consumed by preflight; emulate SQLite's unlinked inode.
                class Stat:
                    st_mode, st_uid, st_nlink = st.st_mode, st.st_uid, 0
                return Stat()
            return st
        with mock.patch.object(Path, "lstat", unlinked_lstat):
            self.pc._secure_inbox_files()
        os.chmod(shm, 0o644)
        with mock.patch.object(Path, "lstat", unlinked_lstat):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "unsafe inbox file"):
                self.pc._secure_inbox_files()
        os.chmod(shm, 0o600)
        for mode, uid, links in ((0o040600, os.getuid(), 0),
                                 (0o100600, os.getuid() + 1, 0),
                                 (0o100600, os.getuid(), 2)):
            def unsafe_lstat(path, *, _mode=mode, _uid=uid, _links=links):
                st = original(path)
                if path == shm:
                    class Stat:
                        st_mode, st_uid, st_nlink = _mode, _uid, _links
                    return Stat()
                return st
            with mock.patch.object(Path, "lstat", unsafe_lstat):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "unsafe inbox file"):
                    self.pc._secure_inbox_files()
        shm.unlink()
        shm.symlink_to(self.pc.INBOX_DB)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "unsafe inbox file"):
            self.pc._secure_inbox_files()

    def test_v2_cleanup_migration_is_transactional_and_rolls_back(self):
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute(self.pc._inbox_ddl()); db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) VALUES('c','m','{}','t','m','d','pending',1,1)")
        db.commit(); db.close(); os.chmod(self.pc.INBOX_DB, 0o600)
        create = self.pc._create_cleanup_table
        def fail_after_ddl(db):
            create(db)
            raise RuntimeError("synthetic migration fault")
        with mock.patch.object(self.pc, "_create_cleanup_table", fail_after_ddl):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration fault"):
                self.pc.inbox_connect()
        db = sqlite3.connect(self.pc.INBOX_DB)
        self.assertEqual(2, db.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(["inbound_delivery"], [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")])
        self.assertEqual("pending", db.execute("SELECT state FROM inbound_delivery").fetchone()[0])
        db.close()
        db = self.pc.inbox_connect()
        self.assertEqual(4, db.execute("PRAGMA user_version").fetchone()[0])
        db.close()

    def test_v2_migration_queues_accepted_attachments_before_erasing_payload(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file = self.pc.ATTACHMENTS / "att-legacy-private.bin"
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET"); os.chmod(private_file, 0o600)
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute(self.pc._inbox_ddl()); db.execute("PRAGMA user_version=2")
        payload = json.dumps({"attachments": [{"uid": "att-legacy", "filename": "private.bin"}]})
        db.execute("INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) VALUES('c','m',?,'t','m','d','handoff_accepted',1,1)", (payload,))
        db.commit(); db.close(); os.chmod(self.pc.INBOX_DB, 0o600)
        db = self.pc.inbox_connect()
        self.assertFalse(private_file.exists())
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery WHERE message_uid='m'").fetchone()[0])
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        db.close()

    def test_existing_database_mode_is_restricted_and_symlink_rejected(self):
        db = self.pc.inbox_connect(); db.close()
        os.chmod(self.pc.INBOX_DB, 0o644)
        before = self.pc.INBOX_DB.read_bytes()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "permissions"):
            self.pc.inbox_connect()
        self.assertEqual(before, self.pc.INBOX_DB.read_bytes())
        self.assertEqual(0o644, self.pc.INBOX_DB.stat().st_mode & 0o777)
        os.chmod(self.pc.INBOX_DB, 0o600)
        self.pc.INBOX_DB.unlink()
        target = Path(self.tmp.name) / "target"; target.write_bytes(b"")
        self.pc.INBOX_DB.symlink_to(target)
        with self.assertRaises(sqlite3.DatabaseError): self.pc.inbox_connect()

    def test_state_dir_symlink_and_schema_lock_hardlink_are_rejected(self):
        other = Path(self.tmp.name).parent / (Path(self.tmp.name).name + "-target")
        other.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, other, True)
        link = Path(self.tmp.name).parent / (Path(self.tmp.name).name + "-link")
        link.symlink_to(other, target_is_directory=True)
        self.addCleanup(link.unlink, True)
        pc = load("plow_chat_symlink_state", "plow-chat/plow-chat.py",
                  {"PLOW_CHAT_STATE_DIR": str(link), "HOST_ID": "h"})
        with self.assertRaises(sqlite3.DatabaseError): pc.inbox_connect()
        lock = self.pc.STATE_DIR / "inbox-schema.lock"
        lock.write_bytes(b""); os.chmod(lock, 0o600)
        hard = self.pc.STATE_DIR / "lock-hardlink"
        os.link(lock, hard)
        with self.assertRaises(sqlite3.DatabaseError): self.pc.inbox_connect()

    def test_v1_migration_erases_legacy_accepted_payload(self):
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute(self.pc._inbox_ddl())
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'handoff_accepted',1,1)",
                   ("c", "m", '{"body":"private legacy payload"}', "t", "m", "d"))
        db.commit(); db.close(); os.chmod(self.pc.INBOX_DB, 0o600)
        db = self.pc.inbox_connect()
        self.assertEqual(4, db.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery").fetchone()[0])
        db.close()

    def test_logs_never_include_body_secrets_and_accepted_attachment_is_removed(self):
        secret = "SYNTHETIC_BODY_SECRET_7f4a"
        private_file = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name("synthetic-chat", "synthetic-m2", "att-file", "private.bin")
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET")
        os.chmod(private_file, 0o600)
        self.pc.send_to_agent = lambda *a, **k: True
        msg = dict(self.msg, body=secret, attachments=[{"uid": "att-file", "filename": "private.bin"}])
        import contextlib
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.pc.route_message(msg)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("private.bin", output.getvalue())
        self.assertFalse(private_file.exists())

    def test_committed_cleanup_survives_crash_before_unlink_and_replays_after_restart(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name("synthetic-chat", "synthetic-m2", "att-crash", "private.bin")
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET"); os.chmod(private_file, 0o600)
        self.pc.send_to_agent = lambda *a, **k: True
        msg = dict(self.msg, attachments=[{"uid": "att-crash", "filename": "private.bin"}])
        replay = self.pc._replay_attachment_cleanup
        def crash_after_commit(db):
            if db.execute("SELECT 1 FROM attachment_cleanup").fetchone():
                raise self.InjectedCrash("accepted cleanup committed before unlink")
            return replay(db)
        with mock.patch.object(self.pc, "_replay_attachment_cleanup", crash_after_commit):
            with self.assertRaises(self.InjectedCrash):
                self.pc.route_message(msg)
        self.assertTrue(private_file.exists())
        pc2 = self.restart()
        db = pc2.inbox_connect()
        self.assertFalse(private_file.exists())
        self.assertEqual([], db.execute("SELECT path FROM attachment_cleanup").fetchall())
        self.assertEqual("handoff_accepted", db.execute("SELECT state FROM inbound_delivery").fetchone()[0])
        db.close()

    def test_cleanup_oserror_keeps_job_and_restart_replays_idempotently(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name("synthetic-chat", "synthetic-m2", "att-oserror", "private.bin")
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET"); os.chmod(private_file, 0o600)
        self.pc.send_to_agent = lambda *a, **k: True
        msg = dict(self.msg, attachments=[{"uid": "att-oserror", "filename": "private.bin"}])
        with mock.patch.object(os, "unlink", side_effect=PermissionError("synthetic busy")):
            self.pc.route_message(msg)
            db = self.pc.inbox_connect()
            self.assertEqual(1, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
            db.close()
        self.assertTrue(private_file.exists())
        pc2 = self.restart()
        db = pc2.inbox_connect()
        self.assertFalse(private_file.exists())
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        db.close()

    def test_discard_persists_cleanup_before_deleting_pending_reference(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name("synthetic-chat", "synthetic-m2", "att-discard", "private.bin")
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET"); os.chmod(private_file, 0o600)
        msg = dict(self.msg, attachments=[{"uid": "att-discard", "filename": "private.bin"}])
        self.pc.inbox_prepare("synthetic-chat", "synthetic-m2", self.pc.order(msg), msg)
        db = self.pc.inbox_connect(); self.pc._record_attachment(db, "synthetic-chat", "synthetic-m2", "att-discard", str(private_file)); db.close()
        self.pc.inbox_resolve_pending("synthetic-chat", "synthetic-m2", "discard")
        self.assertFalse(private_file.exists())
        db = self.pc.inbox_connect()
        self.assertIsNone(db.execute("SELECT 1 FROM inbound_delivery WHERE message_uid='synthetic-m2'").fetchone())
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        db.close()

    def test_discard_cleanup_commit_survives_crash_before_unlink(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        private_file = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name("synthetic-chat", "synthetic-m2", "att-discard-crash", "private.bin")
        private_file.write_bytes(b"SYNTHETIC_ATTACHMENT_SECRET"); os.chmod(private_file, 0o600)
        msg = dict(self.msg, attachments=[{"uid": "att-discard-crash", "filename": "private.bin"}])
        self.pc.inbox_prepare("synthetic-chat", "synthetic-m2", self.pc.order(msg), msg)
        db = self.pc.inbox_connect(); self.pc._record_attachment(db, "synthetic-chat", "synthetic-m2", "att-discard-crash", str(private_file)); db.close()
        replay = self.pc._replay_attachment_cleanup
        def crash_before_unlink(db):
            if db.execute("SELECT 1 FROM attachment_cleanup").fetchone():
                raise self.InjectedCrash("discard cleanup committed before unlink")
            return replay(db)
        with mock.patch.object(self.pc, "_replay_attachment_cleanup", crash_before_unlink):
            with self.assertRaises(self.InjectedCrash):
                self.pc.inbox_resolve_pending("synthetic-chat", "synthetic-m2", "discard")
        self.assertTrue(private_file.exists())
        pc2 = self.restart()
        db = pc2.inbox_connect()
        self.assertFalse(private_file.exists())
        self.assertEqual([], db.execute("SELECT path FROM attachment_cleanup").fetchall())
        self.assertIsNone(db.execute("SELECT 1 FROM inbound_delivery WHERE message_uid='synthetic-m2'").fetchone())
        db.close()

    def test_cleanup_paths_cannot_escape_private_attachment_directory(self):
        outside = Path(self.tmp.name).parent / (Path(self.tmp.name).name + "-synthetic-outside")
        outside.write_bytes(b"synthetic"); self.addCleanup(outside.unlink, True)
        db = self.pc.inbox_connect()
        db.execute("INSERT INTO attachment_cleanup(path,created_at) VALUES(?,?)", ("../" + outside.name, time.time()))
        self.pc._replay_attachment_cleanup(db)
        self.assertTrue(outside.exists())
        self.assertEqual(1, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        db.close()

    def test_manifest_cleanup_is_scoped_and_ignores_uid_prefix_files(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        owned = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name(
            "synthetic-chat", "synthetic-m2", "a", "private.bin")
        neighbor = self.pc.ATTACHMENTS / "a-b-private.bin"
        owned.write_bytes(b"owned synthetic bytes"); neighbor.write_bytes(b"neighbor synthetic bytes")
        os.chmod(owned, 0o600); os.chmod(neighbor, 0o600)
        self.pc.send_to_agent = lambda *a, **k: True
        msg = dict(self.msg, attachments=[{"uid": "a", "filename": "private.bin"}])
        # Existing scoped file avoids network access and is then durably manifested.
        self.pc.route_message(msg)
        self.assertFalse(owned.exists())
        self.assertTrue(neighbor.exists())

    def test_download_rejects_unsafe_existing_attachment_entries_without_touching_targets(self):
        import types
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        uid = "unsafe-existing"
        dest = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name(
            "synthetic-chat", "synthetic-m2", uid, "private.bin")
        outside = Path(self.tmp.name).parent / (Path(self.tmp.name).name + "-attachment-target")
        outside.write_bytes(b"DO NOT READ OR DELETE")
        self.addCleanup(outside.unlink, True)

        dest.symlink_to(outside)
        with self.assertRaisesRegex(OSError, "unsafe existing attachment"):
            self.pc.download({"uid": uid, "filename": "private.bin"}, "synthetic-chat", "synthetic-m2")
        self.assertEqual(b"DO NOT READ OR DELETE", outside.read_bytes())
        dest.unlink()

        dest.write_bytes(b"synthetic hardlink")
        os.chmod(dest, 0o600)
        hard = dest.with_name("external-hardlink")
        os.link(dest, hard)
        self.addCleanup(hard.unlink, True)
        with self.assertRaisesRegex(OSError, "unsafe existing attachment"):
            self.pc.download({"uid": uid, "filename": "private.bin"}, "synthetic-chat", "synthetic-m2")
        self.assertTrue(dest.exists()); self.assertTrue(hard.exists())
        hard.unlink(); dest.unlink()

        for mode in (0o640, 0o644, 0o700):
            dest.write_bytes(b"synthetic mode")
            os.chmod(dest, mode)
            with self.assertRaisesRegex(OSError, "unsafe existing attachment"):
                self.pc.download({"uid": uid, "filename": "private.bin"}, "synthetic-chat", "synthetic-m2")
            self.assertTrue(dest.exists())
            dest.unlink()

        dest.write_bytes(b"synthetic foreign owner")
        os.chmod(dest, 0o600)
        real_stat = self.pc.os.stat
        def foreign_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == dest.name and kwargs.get("dir_fd") is not None:
                return types.SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 1,
                                             st_nlink=result.st_nlink, st_dev=result.st_dev, st_ino=result.st_ino)
            return result
        with mock.patch.object(self.pc.os, "stat", side_effect=foreign_stat):
            with self.assertRaisesRegex(OSError, "unsafe existing attachment"):
                self.pc.download({"uid": uid, "filename": "private.bin"}, "synthetic-chat", "synthetic-m2")
        self.assertTrue(dest.exists())

    def test_duplicate_attachment_uids_are_rejected_before_download_or_inbox_claim(self):
        for names in (("one.bin", "two.bin"), ("one.jpg", "two.png")):
            msg = dict(self.msg, attachments=[{"uid": "same", "filename": n, "url": "/never"} for n in names])
            with mock.patch.object(self.pc, "download") as dl:
                self.assertEqual("ERROR invalid attachment identifiers", self.pc.route_message(msg))
                dl.assert_not_called()
            db = self.pc.inbox_connect()
            self.assertIsNone(db.execute("SELECT 1 FROM inbound_delivery WHERE message_uid=?", (self.msg["uid"],)).fetchone())
            self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_manifest WHERE message_uid=?", (self.msg["uid"],)).fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup WHERE path LIKE 'm-%'").fetchone()[0])
            db.close()

    def test_manifest_path_conflict_fails_before_download(self):
        att = {"uid": "stable", "filename": "new.jpg", "url": "/never"}
        db = self.pc.inbox_connect()
        self.pc._record_attachment(db, "synthetic-chat", self.msg["uid"], "stable", "/other/old.bin")
        db.close()
        with mock.patch.object(self.pc, "download") as dl:
            self.assertEqual("ERROR attachment manifest conflict", self.pc.route_message(dict(self.msg, attachments=[att])))
            dl.assert_not_called()
        db = self.pc.inbox_connect()
        self.assertEqual(1, db.execute("SELECT count(*) FROM attachment_manifest WHERE message_uid=?", (self.msg["uid"],)).fetchone()[0])
        self.assertIsNone(db.execute("SELECT 1 FROM inbound_delivery WHERE message_uid=?", (self.msg["uid"],)).fetchone())
        db.close()

    def test_manifest_write_failure_prevents_download_and_file_creation(self):
        att = {"uid": "db-failure", "filename": "private.bin", "url": "/never"}
        with mock.patch.object(self.pc, "_record_attachment", side_effect=sqlite3.OperationalError("synthetic db failure")), \
                mock.patch.object(self.pc, "download") as dl:
            self.pc.send_to_agent = lambda *a, **k: True
            self.pc.route_message(dict(self.msg, attachments=[att]))
            dl.assert_not_called()
        expected = self.pc.ATTACHMENTS / self.pc._scoped_attachment_name(
            "synthetic-chat", self.msg["uid"], "db-failure", "private.bin")
        self.assertFalse(expected.exists())

    def test_discard_cannot_remove_files_for_rejected_duplicate_uid_envelope(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        atts = [{"uid": "same", "filename": "one.bin"}, {"uid": "same", "filename": "two.jpg"}]
        paths = [self.pc.ATTACHMENTS / self.pc._scoped_attachment_name(
            "synthetic-chat", self.msg["uid"], "same", item["filename"]) for item in atts]
        for path in paths:
            path.write_bytes(b"synthetic protected file"); os.chmod(path, 0o600)
        self.assertEqual("ERROR invalid attachment identifiers",
                         self.pc.route_message(dict(self.msg, attachments=atts)))
        with self.assertRaisesRegex(ValueError, "no unresolved pending row"):
            self.pc.inbox_resolve_pending("synthetic-chat", self.msg["uid"], "discard")
        self.assertTrue(all(path.exists() for path in paths))
        db = self.pc.inbox_connect()
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_manifest").fetchone()[0])
        db.close()

    def test_v3_migration_preserves_duplicate_uid_files_without_manifest_or_cleanup(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        files = [self.pc.ATTACHMENTS / "same-first.bin", self.pc.ATTACHMENTS / "same-second.jpg"]
        for path in files:
            path.write_bytes(b"synthetic duplicate"); os.chmod(path, 0o600)
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute(self.pc._inbox_ddl()); db.execute(self.pc._cleanup_ddl()); db.execute("PRAGMA user_version=3")
        payload = json.dumps({"attachments": [{"uid": "same", "filename": "first.bin"},
                                               {"uid": "same", "filename": "second.jpg"}]})
        db.execute("INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) VALUES('c','duplicate',?,'t','duplicate','d','handoff_accepted',1,1)", (payload,))
        db.commit(); db.close(); os.chmod(self.pc.INBOX_DB, 0o600)
        db = self.pc.inbox_connect()
        self.assertTrue(all(path.exists() for path in files))
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_manifest WHERE message_uid='duplicate'").fetchone()[0])
        self.assertEqual(0, db.execute("SELECT count(*) FROM attachment_cleanup").fetchone()[0])
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery WHERE message_uid='duplicate'").fetchone()[0])
        db.close()

    def test_v3_migration_preserves_prefix_and_sanitization_collisions(self):
        self.pc.ATTACHMENTS.mkdir(mode=0o700)
        safe = self.pc.ATTACHMENTS / "a-private.bin"
        prefix_neighbor = self.pc.ATTACHMENTS / "a-b-private.bin"
        collision = self.pc.ATTACHMENTS / "a_-private.bin"
        for path in (safe, prefix_neighbor, collision):
            path.write_bytes(b"synthetic"); os.chmod(path, 0o600)
        db = sqlite3.connect(self.pc.INBOX_DB)
        db.execute(self.pc._inbox_ddl()); db.execute(self.pc._cleanup_ddl())
        db.execute("PRAGMA user_version=3")
        rows = [
            ("chat-a", "msg-a", [{"uid": "a", "filename": "private.bin"}], "handoff_accepted"),
            ("chat-ab", "msg-ab", [{"uid": "a-b", "filename": "private.bin"}], "pending"),
            ("chat-x", "msg-x", [{"uid": "a!", "filename": "private.bin"}], "handoff_accepted"),
            ("chat-y", "msg-y", [{"uid": "a?", "filename": "private.bin"}], "handoff_accepted"),
        ]
        for chat, mid, atts, state in rows:
            payload = json.dumps({"uid": mid, "attachments": atts})
            db.execute("INSERT INTO inbound_delivery(chat_uid,message_uid,message_json,order_ts,order_uid,delivery_id,state,created_at,updated_at) VALUES(?,?,?,'t',?,? ,?,1,1)",
                       (chat, mid, payload, mid, f"delivery-{mid}", state))
        db.commit(); db.close(); os.chmod(self.pc.INBOX_DB, 0o600)
        db = self.pc.inbox_connect()
        self.assertFalse(safe.exists())
        self.assertTrue(prefix_neighbor.exists())
        self.assertTrue(collision.exists())
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery WHERE message_uid='msg-x'").fetchone()[0])
        self.assertEqual(1, db.execute("SELECT count(*) FROM attachment_manifest WHERE message_uid='msg-ab'").fetchone()[0])
        db.close()

    def test_stale_pending_alert_is_redacted_and_discard_purges_payload(self):
        self.pc.inbox_prepare("synthetic-chat", "synthetic-m2", self.pc.order(self.msg), self.msg)
        db = self.pc.inbox_connect()
        db.execute("UPDATE inbound_delivery SET created_at=?", (time.time() - 90000,))
        db.close()
        alerts = []
        self.pc.send_to_agent = lambda body, agent=None: alerts.append(body) or True
        self.pc.inbox_pending_messages("synthetic-chat")
        self.assertEqual(1, len(alerts))
        self.assertIn("older than 24h", alerts[0])
        self.assertNotIn("synthetic request", alerts[0])
        self.pc.inbox_resolve_pending("synthetic-chat", "synthetic-m2", "discard")
        self.assertIsNone(self.pc.inbox_status("synthetic-chat", "synthetic-m2"))

    def test_wal_checkpoint_is_best_effort_while_reader_holds_snapshot(self):
        self.pc.inbox_prepare("synthetic-chat", "synthetic-m2", self.pc.order(self.msg), self.msg)
        reader = self.pc.inbox_connect()
        reader.execute("BEGIN")
        self.assertIn("synthetic request", reader.execute("SELECT message_json FROM inbound_delivery").fetchone()[0])
        self.pc.inbox_finish("synthetic-chat", "synthetic-m2", "handoff_accepted")
        wal = Path(str(self.pc.INBOX_DB) + "-wal")
        self.assertTrue(wal.exists())
        db = self.pc.inbox_connect()
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery").fetchone()[0])
        db.close()
        reader.close()
        db = self.pc.inbox_connect()
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.assertEqual(0, result[0])
        db.close()

    def test_cross_process_open_and_sigkill_recovery(self):
        plugin = str(Path(__file__).resolve().parents[2] / "mypeople/runtime/plugins/plow-chat/plow-chat.py")
        open_code = "import runpy,sys; n=runpy.run_path(sys.argv[1]); d=n['inbox_connect'](); print(d.execute('PRAGMA user_version').fetchone()[0]); d.close()"
        env = dict(os.environ, PLOW_CHAT_STATE_DIR=self.tmp.name, HOST_ID="h")
        procs = [subprocess.Popen([sys.executable, "-c", open_code, plugin], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [p.communicate(timeout=10) + (p.returncode,) for p in procs]
        self.assertEqual([0, 0], [r[2] for r in results], results)
        self.assertEqual(["4", "4"], [r[0].strip() for r in results])

        ready = Path(self.tmp.name) / "send-started"
        crash_code = ("import os,runpy,sys,time; n=runpy.run_path(sys.argv[1]); "
                      "defsend=lambda *a,**k:(open(sys.argv[2],'w').close(),time.sleep(60))[1]; "
                      "n['route_message'].__globals__['send_to_agent']=defsend; "
                      "print(n['route_message']({'uid':'synthetic-kill','direction':'inbound','body':'secret',"
                      "'chat_uid':'synthetic-chat','created_at':'2026-11-21T12:00:03Z'}),flush=True)")
        proc = subprocess.Popen([sys.executable, "-c", crash_code, plugin, str(ready)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline and proc.poll() is None:
            time.sleep(0.02)
        if not ready.exists():
            out, err = proc.communicate(timeout=5)
            self.fail(f"subprocess did not reach send handoff (rc={proc.returncode}): {err or out}")
        proc.kill(); proc.wait(timeout=5)
        proc.communicate(timeout=2)
        recovered = self.restart()
        sent = []
        recovered.send_to_agent = lambda body, agent=None: sent.append(body) or True
        replay = dict(self.msg, uid="synthetic-kill", created_at="2026-11-21T12:00:03Z")
        self.assertTrue(recovered.route_message(replay).startswith("ROUTE"))
        self.assertEqual("handoff_accepted", recovered.inbox_status("synthetic-chat", "synthetic-kill")[3])
        self.assertEqual(1, len(sent))

    def test_accepted_payload_is_erased_and_retryable_payload_is_retained(self):
        self.pc.send_to_agent = lambda *a, **k: False
        self.pc.route_message(self.msg)
        db = self.pc.inbox_connect()
        self.assertNotEqual("", db.execute("SELECT message_json FROM inbound_delivery").fetchone()[0])
        db.close()
        self.pc.send_to_agent = lambda *a, **k: True
        self.pc.route_message(self.msg)
        db = self.pc.inbox_connect()
        row = db.execute("SELECT message_json,chat_uid,message_uid,delivery_id,state FROM inbound_delivery").fetchone()
        self.assertEqual(("", "synthetic-chat", "synthetic-m2", self.pc.inbox_status("synthetic-chat", "synthetic-m2")[0], "handoff_accepted"), row)
        db.close()

    def test_crash_before_send_recovers_pending_with_same_delivery_id(self):
        def crash_before_send(*a, **k):
            raise self.InjectedCrash("before mocked transport accepted message")
        self.pc.send_to_agent = crash_before_send
        with self.assertRaises(self.InjectedCrash):
            self.pc.route_message(self.msg)
        before = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("dispatching", before[3])
        self.assertNotIn("synthetic-chat", self.pc.read_json(self.pc.STATE, {}).get("marks", {}))

        pc2 = self.restart()
        sent = []
        pc2.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertTrue(pc2.route_message(self.msg).startswith("ROUTE"))
        after = pc2.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("handoff_accepted", after[3])
        self.assertEqual(2, after[2])
        self.assertIn(before[0], sent[0])

    def test_pending_payload_recovers_after_it_ages_off_api_page(self):
        self.pc.api = lambda *a, **k: (200, {"data": []})
        self.pc.poll_chat({"token": "t"}, "synthetic-chat", seed=True)
        old = dict(self.msg, uid="synthetic-old", created_at="2026-11-21T11:59:00Z")
        self.pc.api = lambda *a, **k: (200, {"data": [old]})
        def crash_before_send(*a, **k):
            raise self.InjectedCrash("before mocked transport accepted message")
        self.pc.send_to_agent = crash_before_send
        with self.assertRaises(self.InjectedCrash):
            self.pc.poll_chat({"token": "t"}, "synthetic-chat", seed=False)

        pc2 = self.restart()
        newer = dict(self.msg, uid="synthetic-new", created_at="2026-11-21T12:01:00Z")
        # Simulate Plow pagination: old message no longer appears in current API response.
        pc2.api = lambda *a, **k: (200, {"data": [newer]})
        sent = []
        pc2.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertEqual(2, pc2.poll_chat({"token": "t"}, "synthetic-chat", seed=False))
        self.assertIn("synthetic-old", sent[0])
        self.assertIn("synthetic-new", sent[1])
        self.assertEqual("handoff_accepted", pc2.inbox_status("synthetic-chat", "synthetic-old")[3])
        self.assertEqual("handoff_accepted", pc2.inbox_status("synthetic-chat", "synthetic-new")[3])

    def test_crash_after_transport_acceptance_replays_same_id_at_least_once(self):
        sent = []
        self.pc.send_to_agent = lambda body, agent=None: sent.append(body) or True
        finish = self.pc.inbox_finish
        def crash_before_ack(chat, mid, state, error=""):
            if state == "handoff_accepted":
                raise self.InjectedCrash("transport accepted; durable acknowledgment not written")
            return finish(chat, mid, state, error)
        self.pc.inbox_finish = crash_before_ack
        with self.assertRaises(self.InjectedCrash):
            self.pc.route_message(self.msg)
        first = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("dispatching", first[3])

        pc2 = self.restart()
        pc2.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertTrue(pc2.route_message(self.msg).startswith("ROUTE"))
        second = pc2.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("handoff_accepted", second[3])
        self.assertEqual(2, len(sent))  # transport has no idempotency contract; replay may duplicate
        self.assertIn(first[0], sent[0])
        self.assertIn(first[0], sent[1])
        self.assertNotEqual(first[1], second[1])

    def test_refusal_rolls_back_to_retryable_state_without_advancing_mark(self):
        self.pc.send_to_agent = lambda *a, **k: False
        self.assertTrue(self.pc.route_message(self.msg).startswith("ERROR"))
        refused = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("refused", refused[3])
        self.assertNotIn("synthetic-chat", self.pc.read_json(self.pc.STATE, {}).get("marks", {}))
        sent = []
        self.pc.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertTrue(self.pc.route_message(self.msg).startswith("ROUTE"))
        accepted = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("handoff_accepted", accepted[3])
        self.assertEqual(refused[0], accepted[0])
        self.assertNotEqual(refused[1], accepted[1])

    def test_timeout_is_ambiguous_and_retried_with_same_delivery_id(self):
        self.pc.send_to_agent = lambda *a, **k: None
        self.assertTrue(self.pc.route_message(self.msg).startswith("ERROR"))
        timed_out = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual("ambiguous", timed_out[3])
        self.assertNotIn("synthetic-chat", self.pc.read_json(self.pc.STATE, {}).get("marks", {}))
        sent = []
        self.pc.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertTrue(self.pc.route_message(self.msg).startswith("ROUTE"))
        accepted = self.pc.inbox_status("synthetic-chat", "synthetic-m2")
        self.assertEqual(timed_out[0], accepted[0])
        self.assertNotEqual(timed_out[1], accepted[1])

    def test_duplicate_replay_after_acceptance_does_not_send_twice(self):
        sent = []
        self.pc.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertTrue(self.pc.route_message(self.msg).startswith("ROUTE"))
        self.assertEqual("skip: already routed", self.pc.route_message(self.msg))
        self.assertEqual(1, len(sent))
        db = self.pc.inbox_connect()
        self.assertEqual("", db.execute("SELECT message_json FROM inbound_delivery").fetchone()[0])
        db.close()
        self.assertEqual(["2026-11-21T12:00:02.000000Z", "synthetic-m2"],
                         self.pc.read_json(self.pc.STATE, {})["marks"]["synthetic-chat"])

    def test_concurrent_duplicate_routes_serialize_to_one_send(self):
        entered, release = threading.Event(), threading.Event()
        sent, results = [], []
        def delayed_send(body, agent=None):
            sent.append(body)
            entered.set()
            self.assertTrue(release.wait(3))
            return True
        self.pc.send_to_agent = delayed_send
        one = threading.Thread(target=lambda: results.append(self.pc.route_message(self.msg)))
        two = threading.Thread(target=lambda: results.append(self.pc.route_message(self.msg)))
        one.start()
        self.assertTrue(entered.wait(3))
        two.start()
        time.sleep(0.05)
        release.set()
        one.join(3); two.join(3)
        self.assertFalse(one.is_alive()); self.assertFalse(two.is_alive())
        self.assertEqual(1, len(sent))
        self.assertEqual(1, sum(r.startswith("ROUTE") for r in results))
        self.assertEqual(1, sum(r == "skip: already routed" for r in results))

    def test_poll_sorts_messages_before_advancing_per_chat_high_water(self):
        older = dict(self.msg, uid="synthetic-m1", created_at="2026-11-21T12:00:01Z", body="older")
        newer = dict(self.msg, uid="synthetic-m3", created_at="2026-11-21T12:00:03Z", body="newer")
        msgs = []
        self.pc.api = lambda *a, **k: (200, {"data": msgs})
        sent = []
        self.pc.send_to_agent = lambda body, agent=None: sent.append(body) or True
        self.assertEqual(0, self.pc.poll_chat({"token": "t"}, "synthetic-chat", seed=True))
        msgs[:] = [newer, older]
        # Reversed API order is sorted and routed oldest first after startup seeding.
        self.assertEqual(2, self.pc.poll_chat({"token": "t"}, "synthetic-chat", seed=False))
        stamp = "[plowchat] from synthetic in synthetic-chat:"
        delivered = [line for envelope in sent for line in envelope.splitlines()
                     if line.startswith(stamp)]
        self.assertEqual([f"{stamp} older", f"{stamp} newer"], delivered)
        self.assertEqual(["2026-11-21T12:00:03.000000Z", "synthetic-m3"],
                         self.pc.read_json(self.pc.STATE, {})["marks"]["synthetic-chat"])


class CloudTest(unittest.TestCase):
    """A 1-click Plow VM: no saved login, PLOW_API_BASE + a proxied token."""

    def test_cloud_agent_reads_chats_from_me_and_says_hello_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            pc = load("plow_chat_cloud", "plow-chat/plow-chat.py",
                      {"PLOW_CHAT_STATE_DIR": tmp, "HOST_ID": "h",
                       "PLOW_API_BASE": "https://plow-x.int.exe.xyz/"})
            with mock.patch.dict(os.environ, {"PLOW_API_BASE": "https://plow-x.int.exe.xyz/"}):
                os.environ.pop("PLOW_AGENT_TOKEN", None)
                creds = pc.load_creds()
            self.assertEqual(pc.BASE, "https://plow-x.int.exe.xyz")
            self.assertEqual(creds["token"], "proxied")
            calls = []

            def api(method, path, body=None, token=None):
                calls.append((method, path, body, token))
                if path == "/v1/agents/me":
                    return 200, {"chats": [{"uid": "cht_c"}]}
                return 200, {"data": [{"uid": "m1", "direction": "inbound",
                                       "body": "Set this up for me", "chat_uid": "cht_c"}]}
            pc.api = api
            self.assertEqual(pc.list_chats(creds), ["cht_c"])
            pc.poll_chat(creds, "cht_c")
            pc.poll_chat(creds, "cht_c")
            hellos = [c for c in calls if c[0] == "POST"]
            self.assertEqual(hellos, [("POST", "/v1/chats/cht_c/messages",
                                       {"body": pc.WELCOME}, "proxied")])

    def test_routed_myplow_sends_no_generic_welcome(self):
        """Build an Agent routes a person's own MyPlow (creds carry owner_handle); its Boss greets them itself, so the
        bridge's generic hello would be a second, worse greeting (Daniel 10-28)."""
        with tempfile.TemporaryDirectory() as tmp:
            pc = load("plow_chat_routed", "plow-chat/plow-chat.py", {"PLOW_CHAT_STATE_DIR": tmp, "HOST_ID": "h"})
            creds = {"token": "tok", "chat_uid": "", "cloud": True, "owner_handle": "+15550100001", "line_uid": "ln_p8"}
            calls = []

            def api(method, path, body=None, token=None):
                calls.append((method, path))
                return 200, {"data": [{"uid": "m1", "direction": "inbound", "body": "Plow Activate: X", "chat_uid": "cht_r"}]}
            pc.api = api
            pc.poll_chat(creds, "cht_r")
            self.assertEqual([c for c in calls if c[0] == "POST"], [], "no generic hello for a routed MyPlow")


    def test_inbound_is_read_as_the_literal_text(self):
        """Plow's default body is markdown, which puts backslashes before _ - ~: agents must get what
        the owner typed (10-29)."""
        with tempfile.TemporaryDirectory() as tmp:
            pc = load("plow_chat_literal", "plow-chat/plow-chat.py", {"PLOW_CHAT_STATE_DIR": tmp, "HOST_ID": "h"})
            reads = []

            def api(method, path, body=None, token=None):
                if method == "GET":
                    reads.append(path)
                return 200, {"data": []}
            pc.api = api
            pc.poll_chat({"token": "t", "chat_uid": "cht_l"}, "cht_l")
            self.assertEqual(["/v1/chats/cht_l/messages?format=text_decorations"], reads)

    def test_cloud_agent_works_once_plow_drops_the_legacy_me_path(self):
        """Plow keeps GET /v1/agents/cloud/me only until images move off it; the bridge reads /v1/agents/me."""
        with tempfile.TemporaryDirectory() as tmp:
            pc = load("plow_chat_me", "plow-chat/plow-chat.py", {"PLOW_CHAT_STATE_DIR": tmp, "HOST_ID": "h"})

            def api(method, path, body=None, token=None):
                if path == "/v1/agents/me":
                    return 200, {"chats": [{"uid": "cht_m"}], "mcp_url": "https://relay.example/mcp"}
                return 404, {"detail": "Not Found"}
            pc.api = api
            creds = {"token": "t", "chat_uid": "", "cloud": True}
            self.assertEqual(pc.list_chats(creds), ["cht_m"])
            # The old Latch app's relay is retired (Daniel 10-29): the bridge no longer adds it to Claude.
            self.assertFalse(hasattr(pc, "connect_latch"))
            self.assertNotIn("/v1/agents/cloud/me", Path(PLUGINS, "plow-chat", "plow-chat.py").read_text())


class LabBridgeTest(unittest.TestCase):
    def test_a_saved_login_names_its_own_server(self):
        """A cloud VM learns where the owner's WhatsApp lab is from its lab login alone."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "creds.json").write_text(json.dumps({"token": "k", "base_url": "https://lab.example/"}))
            pc = load("plow_chat_lab", "plow-chat/plow-chat.py",
                      {"PLOW_CHAT_STATE_DIR": d, "PLOW_API_BASE": "https://api.plow.co", "HOST_ID": "h"})
            self.assertEqual(pc.BASE, "https://lab.example")
            self.assertIn(f"PLOW_CHAT_STATE_DIR={d} ", pc.RUN, "the Boss's replies must use the same login")

    def test_supervise_runs_the_lab_bridge_only_while_its_login_exists(self):
        sup = (PLUGINS.parent / "bin" / "supervise.sh").read_text()
        line = next(x for x in sup.splitlines() if "plow-chat-lab/creds.json" in x)
        with tempfile.TemporaryDirectory() as idir:
            run = lambda: subprocess.run(["bash", "-c", f'ensure(){{ echo "$1|$2"; }}; ID={idir}; {line}'],
                                         capture_output=True, text=True).stdout
            self.assertEqual("", run())
            Path(idir, "state", "plow-chat-lab").mkdir(parents=True)
            Path(idir, "state", "plow-chat-lab", "creds.json").write_text("{}")
            pat, cmd = run().strip().split("|")
            self.assertTrue(pat.endswith("plow-chat.py serve lab$"))
            self.assertIn(f"PLOW_CHAT_STATE_DIR='{idir}/state/plow-chat-lab' exec python3", cmd)


class WiringTest(unittest.TestCase):
    def test_supervise_starts_it_only_when_turned_on_and_down_stops_it(self):
        root = PLUGINS.parents[2]
        sup = (root / "mypeople" / "runtime" / "bin" / "supervise.sh").read_text()
        self.assertIn("ensure_plugins\n", sup)
        self.assertIn("needs=env:PLOW_CHAT\n", (PLUGINS / "plow-chat" / "plugin.conf").read_text())
        self.assertIn('os.path.join(install, sub) + os.sep', sandbox.implementation("cli.py"))

    def test_supervise_sees_only_the_main_bridge_as_the_main_bridge(self):
        """10-13: a second bridge (the owner's WhatsApp number as its own Plow account) ran the same
        script, supervise's liveness check matched it, and the dead main bridge was never restarted:
        the owner's texts went unanswered for 51 minutes. A labelled second bridge or a one-shot
        `reply` must not count as the main bridge; the main bridge itself still must."""
        fn = re.search(r"^ensure_plugins\(\)\{.*?^\}", (PLUGINS.parent / "bin" / "supervise.sh").read_text(), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as idir:
            script = f"{idir}/plugins/plow-chat/plow-chat.py"
            Path(script).parent.mkdir(parents=True)
            shutil.copy(PLUGINS / "plow-chat" / "plugin.conf", Path(script).parent)
            pat = subprocess.run(["bash", "-c", f'ensure(){{ echo "$1"; }}; ID={idir}; PLOW_CHAT=1; {fn}\nensure_plugins'],
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(f"{script} serve$", pat)

            def running(*argv):
                return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", script, *argv])

            def seen():
                return subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()

            others = [running("serve", "whatsapp"), running("reply", "cht_x", "hi")]
            try:
                self.assertEqual([], seen())
                main = running("serve")
                try:
                    self.assertEqual([str(main.pid)], seen())
                finally:
                    main.kill()
            finally:
                for p in others:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
