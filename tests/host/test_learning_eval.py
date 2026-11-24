import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/learning_eval.py"


def case(event="merge", **facts):
    return {
        "incident": "a" * 32,
        "at": 100,
        "active_policy": None,
        "budgets": {"context_bytes": 4096, "calls": 1, "tool_calls": 0},
        "facts": {
            "repo": "o/p",
            "role": "engineer",
            "event": event,
            "event_id": "b" * 32,
            "observed": "confirmed",
            "exceptions": [],
            "available": True,
            "authorized": True,
            "verified": True,
            "prior_attempts": [],
            "recipients": {"owner": "1" * 32, "worker": "2" * 32},
            **facts,
        },
    }


class OfflineLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def command(self, *args, data=None, check=True):
        with tempfile.TemporaryDirectory(dir=self.root) as clean:
            return subprocess.run(
                [sys.executable, "-I", "-B", str(SCRIPT), *map(str, args)],
                cwd=clean,
                env={"HOME": clean, "TMPDIR": clean, "PATH": os.environ["PATH"]},
                text=True,
                capture_output=True,
                input=json.dumps(data) if data is not None else None,
                check=check,
                timeout=20,
            )

    def test_fresh_worker_current_baseline_uses_live_policy_code_with_simulated_sends(self):
        item = case(prior_attempts=["worker"])
        results = [json.loads(self.command("worker", "A", data=item).stdout) for _ in range(3)]
        self.assertEqual(3, len({r["session"] for r in results}))
        for result in results:
            self.assertEqual([{"role": "owner", "id": "1" * 32, "result": "simulated"}], result["attempts"])
            self.assertIsNone(result["cost"])
            self.assertEqual(0, result["resources"]["network_calls"])

    def test_fixed_rule_sends_mandatory_ci_and_abstains_if_denied(self):
        ci = json.loads(self.command("worker", "B", data=case("ci_failure")).stdout)
        self.assertEqual("worker", ci["attempts"][0]["role"])
        denied = json.loads(self.command("worker", "B", data=case("ci_failure", available=False)).stdout)
        self.assertEqual("abstain", denied["action"])
        self.assertFalse(denied["attempts"])

    def test_worker_rejects_labels_and_other_arm_snapshots(self):
        injected = {**case(), "label": "notify_owner"}
        self.assertNotEqual(0, self.command("worker", "A", data=injected, check=False).returncode)
        self.assertNotEqual(0, self.command("worker", "B", "/fixture.db", data=case(), check=False).returncode)

    def test_scorer_flags_mandatory_omissions_and_incomplete_metering(self):
        data = case("ci_failure")
        traces, labels = self.root / "traces.jsonl", self.root / "labels.jsonl"
        labels.write_text(
            json.dumps(
                {
                    "incident": data["incident"],
                    "action": "route_worker",
                    "state": {"attempted_roles": ["worker"], "abstained": False},
                    "destinations": [{"role": "worker", "id": "2" * 32}],
                    "critical": ["mandatory_omission"],
                }
            )
            + "\n"
        )
        with traces.open("w") as out:
            for rep in (1, 2, 3):
                for arm in ("A", "B", "C"):
                    behavior = "B" if arm != "A" else "A"
                    row = json.loads(self.command("worker", behavior, data=data).stdout)
                    if arm == "C" and rep == 1:
                        row["attempts"], row["attempt_count"] = [], 0
                    out.write(
                        json.dumps(
                            {
                                **row,
                                "incident": data["incident"],
                                "arm": arm,
                                "repetition": rep,
                                "evaluator_sha": "2" * 40,
                                "implementation_sha": "1" * 40,
                            }
                        )
                        + "\n"
                    )
        result = json.loads(
            self.command("score", traces, labels, "--baseline", "B", "--minimum-incidents", "20").stdout
        )
        self.assertEqual("critical_failure", result["result"])
        self.assertEqual(1, result["arms"]["C"]["missed"])
        self.assertIsNone(result["arms"]["B"]["cost_per_correct"])

    def test_memory_uses_approved_snapshot_and_filters_scope_and_exception(self):
        sys.path.insert(0, str(ROOT / "mypeople/runtime/bin"))
        self.addCleanup(sys.path.remove, str(ROOT / "mypeople/runtime/bin"))
        import mplearning as learning

        snapshot = self.root / "approved.sqlite3"
        store = learning.LearningStore(snapshot, clock=lambda: 100)

        def act(origin, op, data):
            return store.execute(
                learning.Grant(origin, frozenset({("o/p", "engineer")})),
                {"op": op, "repo": "o/p", "role": "engineer", "data": data},
            )

        incident = "a" * 32
        act(
            "owner",
            "record",
            {
                "incident": incident,
                "event": "ci_failure",
                "observed": "confirmed",
                "decision": "route_worker",
                "outcome": "confirmed",
                "evidence": "b" * 32,
            },
        )
        lesson = act(
            "worker",
            "propose",
            {
                "lesson": "c" * 32,
                "incident": incident,
                "event": "ci_failure",
                "condition": "confirmed",
                "action": "route_worker",
                "exceptions": ["unknown"],
                "valid_from": 0,
                "expires": 200,
            },
        )
        for who, state in (("evaluator", "evaluated"), ("owner", "shadow"), ("owner", "active")):
            act(who, "transition", {**lesson, "state": state})
        for changes, matched in (({}, True), ({"repo": "o/another"}, False), ({"exceptions": ["unknown"]}, False)):
            row = json.loads(self.command("worker", "C", snapshot, data=case("ci_failure", **changes)).stdout)
            self.assertEqual(matched, bool(row["lessons"]))
            self.assertEqual(matched, bool(row["attempts"]))

    def evaluator_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("learning_eval_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_parent_preserves_intent_after_worker_crash_and_timeout(self):
        module = self.evaluator_module()
        frame = {"kind": "attempt", "data": {"role": "owner", "id": "1" * 32, "result": "simulated"}}
        for tail, timeout in (("raise SystemExit(3)", 3), ("import time; time.sleep(30)", 0.5)):
            code = f"print({json.dumps(json.dumps(frame))},flush=True); {tail}"
            result = module.run_framed([sys.executable, "-c", code], str(self.root), case(), timeout)
            self.assertEqual([frame["data"]], result["attempts"])
            self.assertEqual(1, result["attempt_count"])
            self.assertEqual("worker_failed", result["error"])
            self.assertEqual(timeout < 1, result["timeout"])

    def test_complete_state_attempt_count_and_full_latency_are_required(self):
        item = case()
        trace = json.loads(self.command("worker", "B", data=item).stdout)
        label = {
            "incident": item["incident"],
            "action": trace["action"],
            "state": trace["state"],
            "destinations": [{"role": a["role"], "id": a["id"]} for a in trace["attempts"]],
            "critical": [],
        }
        traces, labels = self.root / "observations", self.root / "labels"
        labels.write_text(json.dumps(label) + "\n")
        records = []
        for arm in ("A", "B", "C"):
            for rep in (1, 2, 3):
                row = {
                    **trace,
                    "incident": item["incident"],
                    "arm": arm,
                    "repetition": rep,
                    "session": f"{len(records):032x}",
                    "implementation_sha": "1" * 40,
                    "evaluator_sha": "2" * 40,
                    "cost": 1,
                }
                records.append(row)
        records[-1]["state"] = {"attempted_roles": ["owner"], "abstained": False}
        records[-1]["latency_ns"] = None
        traces.write_text("".join(json.dumps(row) + "\n" for row in records))
        args = ("score", traces, labels, "--baseline", "B", "--minimum-incidents", "1")
        result = json.loads(self.command(*args).stdout)
        self.assertEqual(2, result["arms"]["C"]["correct"])
        self.assertEqual(2, result["arms"]["C"]["timed_trials"])
        self.assertIsNone(result["arms"]["C"]["p95_latency_ns"])
        self.assertEqual("inconclusive", result["result"])
        records[-1]["attempt_count"] = 100
        traces.write_text("".join(json.dumps(row) + "\n" for row in records))
        self.assertNotEqual(0, self.command(*args, check=False).returncode)
