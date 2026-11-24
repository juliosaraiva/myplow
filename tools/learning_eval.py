#!/usr/bin/env python3
"""Offline paired comparison for the notification policy. Never opens a network connection."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "mypeople/runtime/plugins/github-prs"
sys.path[:0] = [str(ROOT / "mypeople/runtime/bin"), str(PLUGIN)]
import mplearning as learning  # noqa: E402
from supervisor_ledger import Ledger  # noqa: E402

ARMS = ("A", "B", "C")
MODEL = "deterministic-reference-worker"
ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")
CRITICAL = {"cross_scope", "unauthorized_attempt", "mandatory_omission"}
OWNER = "notify_owner"
WORKER = "route_worker"
SEND_ROLES = {OWNER: "owner", WORKER: "worker"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition):
    if not condition:
        raise ValueError("invalid offline evaluation input")


def opaque(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def keys(value, fields):
    require(isinstance(value, dict) and value.keys() == set(fields.split()))


def validate_case(case):
    keys(case, "incident at facts active_policy budgets")
    require(opaque(case["incident"]) and type(case["at"]) is int and 0 <= case["at"] < 2**40)
    facts = case["facts"]
    keys(facts, "repo role event event_id observed exceptions available verified authorized recipients prior_attempts")
    require(isinstance(facts["repo"], str) and bool(learning.REPO.fullmatch(facts["repo"])))
    require(isinstance(facts["role"], str) and bool(learning.ROLE.fullmatch(facts["role"])))
    require(opaque(facts["event_id"]) and facts["event"] in learning.EVENTS and facts["observed"] in learning.STATES)
    require(isinstance(facts["exceptions"], list) and len(facts["exceptions"]) <= len(learning.STATES))
    require(all(flag in learning.STATES for flag in facts["exceptions"]))
    require(all(type(facts[key]) is bool for key in ("available", "verified", "authorized")))
    keys(facts["recipients"], "owner worker")
    require(all(value is None or opaque(value) for value in facts["recipients"].values()))
    require(isinstance(facts["prior_attempts"], list) and len(facts["prior_attempts"]) <= 2)
    require(all(value in SEND_ROLES.values() for value in facts["prior_attempts"]))
    require(case["active_policy"] is None or case["active_policy"] in learning.ACTIONS)
    keys(case["budgets"], "context_bytes calls tool_calls")
    require(all(type(value) is int and 0 <= value <= 65_536 for value in case["budgets"].values()))


def current_actions(case):
    """Exercise current supervisor action production against a frozen synthetic PR projection."""
    facts = case["facts"]
    event = facts["event"]
    if event not in ("merge", "review") or not facts["verified"]:
        return []
    repo = facts["repo"]
    db = Ledger(Path("supervisor.sqlite"), clock=lambda: case["at"])
    db.track(repo, 1)
    db.apply(
        repo,
        1,
        1,
        {
            "head": "1" * 40,
            "base": "2" * 40,
            "open": event == "review",
            "draft": False,
            "merged": "3" * 40 if event == "merge" else "",
            "approval": None,
            "checks": [],
            "threads": [1] if event == "review" else [],
            "copilot": "none",
        },
    )
    mapping = {"pr_merged": OWNER, "pr_merged_owner": WORKER, "review": WORKER}
    actions = []
    while action := db.claim():
        actions.append(mapping[action["kind"]])
        require(db.begin(action))
        require(db.finish(action, "sent", receipt="simulated"))
    return actions


def fixed_actions(facts):
    event = facts["event"]
    if event == "direct_request":
        return [OWNER]
    if facts["observed"] == "duplicate":
        return []
    if facts["verified"] and event == "merge":
        return [OWNER, WORKER]
    if facts["verified"] and event in ("review", "ci_failure"):
        return [WORKER]
    return []


def memory_lookup(case, snapshot):
    facts = case["facts"]
    grant = learning.Grant("worker", frozenset({(facts["repo"], facts["role"])}))
    local = Path("trial-lessons.sqlite")
    shutil.copyfile(snapshot, local)
    local.chmod(0o600)
    return learning.LearningStore(local, clock=lambda: case["at"]).execute(
        grant,
        {
            "op": "lookup",
            "repo": facts["repo"],
            "role": facts["role"],
            "data": {key: facts[key] for key in ("event", "observed", "exceptions")},
        },
    )


def trial(case, arm, snapshot, emit=None):
    validate_case(case)
    facts = case["facts"]
    attempts, consulted = [], []
    context = {**case, "arm": arm}
    reason, selected = "policy", []
    start = time.perf_counter_ns()
    cpu = time.process_time_ns()
    if not facts["authorized"] or not facts["available"]:
        reason, selected = "unverified_or_denied", ["abstain"]
    elif case["active_policy"] is not None:
        reason, selected = "explicit", [case["active_policy"]]
    elif arm == "B":
        reason, selected = "fixed", fixed_actions(facts)
    else:
        if arm == "C":
            result = memory_lookup(case, snapshot)
            consulted = result["consulted"]
            reason = result["reason"]
            context["consulted"] = consulted
            if result["action"]:
                selected = [result["action"]]
        if not selected:
            selected = current_actions(case)
    context_bytes = len(json.dumps(context, separators=(",", ":")).encode())
    budget_error = context_bytes > case["budgets"]["context_bytes"] or case["budgets"]["calls"] < 1
    prior = set(facts["prior_attempts"])
    if not budget_error:
        for action in selected:
            role = SEND_ROLES.get(action)
            if role is None or role in prior:
                continue
            destination = facts["recipients"][role]
            attempt = {"role": role, "id": destination, "result": "simulated" if destination else "no_destination"}
            if emit:
                emit("attempt", attempt)
            attempts.append(attempt)
            if destination:
                prior.add(role)
    return {
        "action": selected[0] if selected else "silence",
        "reason": reason,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "state": {"attempted_roles": sorted(prior), "abstained": "abstain" in selected},
        "error": "budget_exhausted" if budget_error else None,
        "timeout": False,
        "latency_ns": time.perf_counter_ns() - start,
        "resources": {
            "cpu_ns": time.process_time_ns() - cpu,
            "context_bytes": context_bytes,
            "model_calls": 0,
            "network_calls": 0,
            "simulated_attempts": len(attempts),
        },
        "model": MODEL,
        "cost": None,
        "prompt_digest": digest(
            {"worker": MODEL, "arm": arm, "policy": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        ),
        "lessons": consulted,
        "lesson_digest": digest(consulted),
        "worker": str(os.getpid()),
        "session": uuid.uuid4().hex,
    }


def read_rows(path):
    with Path(path).open() as source:
        for line in source:
            require(len(line) <= 65_536)
            yield json.loads(line)


def validate_attempt(attempt):
    keys(attempt, "role id result")
    require(attempt["role"] in SEND_ROLES.values())
    require(
        (opaque(attempt["id"]) and attempt["result"] == "simulated")
        or (attempt["id"] is None and attempt["result"] == "no_destination")
    )


def run_framed(command, session, case, timeout):
    """Parent receives attempted delivery intents even if the worker never returns a result."""
    attempts, result = [], None
    expired = False
    try:
        proc = subprocess.run(
            command,
            cwd=session,
            env={"HOME": session, "TMPDIR": session},
            input=json.dumps(case),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw, failed = proc.stdout, proc.returncode != 0
    except subprocess.TimeoutExpired as error:
        raw, failed, expired = (error.stdout or b"").decode(errors="replace"), True, True
    except OSError:
        raw, failed = "", True
    for line in raw.splitlines(keepends=True):
        if not line.endswith("\n"):
            failed = True
            break
        try:
            frame = json.loads(line)
            keys(frame, "kind data")
            if frame["kind"] == "attempt" and result is None:
                validate_attempt(frame["data"])
                attempts.append(frame["data"])
            elif frame["kind"] == "result" and result is None:
                result = frame["data"]
                require(isinstance(result, dict))
            else:
                raise ValueError("invalid frame")
        except (ValueError, TypeError, KeyError):
            failed = True
    if not failed and result is not None:
        return {**result, "attempts": attempts, "attempt_count": len(attempts)}
    return {
        "error": "worker_failed",
        "timeout": expired,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "action": None,
        "state": None,
        "latency_ns": None,
        "resources": None,
        "cost": None,
        "model": MODEL,
        "prompt_digest": None,
        "lesson_digest": None,
        "lessons": [],
        "worker": None,
        "session": None,
    }


def run(args):
    require(bool(SHA.fullmatch(args.evaluator_sha)))
    implementation = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    require(not subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"]))
    snapshot = Path(args.snapshot).resolve(strict=True)
    require(snapshot.stat().st_size <= 32_000_000)
    require(not any(Path(str(snapshot) + suffix).exists() for suffix in ("-wal", "-journal")))
    snapshot_bytes = snapshot.read_bytes()
    require(len(snapshot_bytes) <= 32_000_000)
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    cases = list(read_rows(args.cases))
    require(0 < len(cases) <= 2000 and len({case["incident"] for case in cases}) == len(cases))
    seed = random.Random(args.seed)
    with Path(args.output).open("x") as output:
        for case in cases:
            validate_case(case)
            for repetition in range(1, 4):
                arms = list(ARMS)
                seed.shuffle(arms)
                trial_seed = seed.randrange(2**32)
                for arm in arms:
                    trace = {
                        "incident": case["incident"],
                        "arm": arm,
                        "repetition": repetition,
                        "seed": trial_seed,
                        "evaluator_sha": args.evaluator_sha,
                        "implementation_sha": implementation,
                        "snapshot_digest": snapshot_digest if arm == "C" else None,
                    }
                    with tempfile.TemporaryDirectory() as session:
                        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "worker", arm]
                        if arm == "C":
                            frozen = Path(session) / "approved-snapshot.sqlite"
                            frozen.write_bytes(snapshot_bytes)
                            frozen.chmod(0o600)
                            command.append(str(frozen))
                        command.extend(("--framed", "--repetition", str(repetition), "--trial-seed", str(trial_seed)))
                        trace.update(run_framed(command, session, case, args.timeout))
                    output.write(json.dumps(trace, sort_keys=True) + "\n")


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(p * len(values)) - 1)] if values else None


def validate_state(state):
    keys(state, "attempted_roles abstained")
    roles = state["attempted_roles"]
    require(isinstance(roles, list) and all(role in SEND_ROLES.values() for role in roles))
    require(roles == sorted(set(roles)) and type(state["abstained"]) is bool)


def score(args):
    require(1 <= args.minimum_incidents <= 2000)
    traces = list(read_rows(args.results))
    labels = {}
    for label in read_rows(args.labels):
        if "eligible_lessons" not in label:
            label["eligible_lessons"] = []
        keys(label, "incident action destinations state critical eligible_lessons")
        require(isinstance(label["eligible_lessons"], list))
        require(all(isinstance(d, str) and learning.HEX.fullmatch(d) for d in label["eligible_lessons"]))
        require(opaque(label["incident"]) and label["incident"] not in labels)
        require(label["action"] in learning.ACTIONS)
        validate_state(label["state"])
        require(all(x in CRITICAL for x in label["critical"]))
        require(isinstance(label["destinations"], list) and len(label["destinations"]) <= 2)
        for destination in label["destinations"]:
            keys(destination, "role id")
            require(destination["role"] in SEND_ROLES.values() and opaque(destination["id"]))
        labels[label["incident"]] = label
    require(labels and len(traces) == len(labels) * len(ARMS) * 3)
    rows = {arm: defaultdict(list) for arm in ARMS}
    counts = {arm: Counter() for arm in ARMS}
    critical = {arm: Counter() for arm in ARMS}
    latency, costs, cpu, sessions, seen = defaultdict(list), defaultdict(list), defaultdict(list), set(), set()
    pins = {key: {row[key] for row in traces} for key in ("implementation_sha", "evaluator_sha", "model")}
    require(all(len(values) == 1 for values in pins.values()))
    for row in traces:
        incident, arm, repetition = (row[key] for key in ("incident", "arm", "repetition"))
        key = (incident, arm, repetition)
        require(key not in seen and incident in labels and arm in ARMS and repetition in (1, 2, 3))
        seen.add(key)
        session = row["session"]
        if session:
            require(opaque(session) and session not in sessions)
            sessions.add(session)
        label = labels[incident]
        require(type(row["attempt_count"]) is int and row["attempt_count"] == len(row["attempts"]))
        for attempt in row["attempts"]:
            validate_attempt(attempt)
        if not row["error"]:
            validate_state(row["state"])
        attempts = [{key: item[key] for key in ("role", "id")} for item in row["attempts"]]
        expected = label["destinations"]
        extra = sum((Counter(map(digest, attempts)) - Counter(map(digest, expected))).values())
        missing = sum((Counter(map(digest, expected)) - Counter(map(digest, attempts))).values())
        accurate = (
            not row["error"]
            and row["action"] == label["action"]
            and attempts == expected
            and row["state"] == label["state"]
        )
        mismatched_scope = {lesson["digest"] for lesson in row["lessons"]} - set(label["eligible_lessons"])
        violations = {
            "cross_scope": bool(mismatched_scope or extra),
            "unauthorized_attempt": bool(extra),
            "mandatory_omission": bool(missing),
        }
        for condition in label["critical"]:
            if violations[condition]:
                critical[arm][condition] += 1
        rows[arm][incident].append(int(accurate))
        counts[arm].update(
            {
                "trials": 1,
                "correct": int(accurate),
                "false_attempts": extra,
                "false_alert_trials": int(bool(extra)),
                "missed": missing,
                "due_attempts": len(expected),
                "attempts": len(attempts),
                "errors": int(bool(row["error"])),
                "timeouts": int(row["timeout"]),
                "abstentions_due": int(label["state"]["abstained"]),
                "correct_abstentions": int(accurate and label["state"]["abstained"]),
            }
        )
        if type(row["latency_ns"]) is int and row["latency_ns"] >= 0:
            latency[arm].append(row["latency_ns"])
        if row["resources"]:
            cpu[arm].append(row["resources"]["cpu_ns"])
        if type(row["cost"]) in (int, float) and row["cost"] >= 0:
            costs[arm].append(row["cost"])
    summary = {}
    for arm in ARMS:
        tally = counts[arm]
        summary[arm] = {
            **dict(tally),
            "critical": dict(critical[arm]),
            "exact_all_three": sum(sum(values) == 3 for values in rows[arm].values()),
            "timed_trials": len(latency[arm]),
            "p95_latency_ns": percentile(latency[arm], 0.95) if len(latency[arm]) == tally["trials"] else None,
            "cpu_ns_total": sum(cpu[arm]),
            "cpu_ns_per_correct": sum(cpu[arm]) / tally["correct"] if tally["correct"] else None,
            "cost_per_correct": (
                sum(costs[arm]) / tally["correct"] if tally["correct"] and len(costs[arm]) == tally["trials"] else None
            ),
        }
    baseline = args.baseline
    pairs = [(key, sum(rows["C"][key]), sum(rows[baseline][key])) for key in sorted(labels)]
    differences = [(c - b) / 3 for _, c, b in pairs]
    gain = sum(differences) / len(differences)
    rng = random.Random(args.seed)
    bootstrap = [sum(rng.choices(differences, k=len(differences))) / len(differences) for _ in range(2000)]
    lower = percentile(bootstrap, 0.025)
    base_cost, candidate_cost = (summary[arm]["cost_per_correct"] for arm in (baseline, "C"))
    old_latency, new_latency = (summary[arm]["p95_latency_ns"] for arm in (baseline, "C"))
    if critical["C"]:
        result = "critical_failure"
    elif (
        len(labels) < args.minimum_incidents
        or base_cost is None
        or candidate_cost is None
        or old_latency is None
        or new_latency is None
    ):
        result = "inconclusive"
    elif (
        gain >= 0.10
        and lower > 0
        and candidate_cost <= base_cost * 1.10
        and old_latency is not None
        and new_latency is not None
        and new_latency <= old_latency * 1.15
        and new_latency - old_latency <= 250_000_000
    ):
        result = "candidate_meets_offline_thresholds"
    else:
        result = "prefer_fixed_rule" if gain == 0 or gain < 0.10 else "candidate_does_not_meet_thresholds"
    print(
        json.dumps(
            {
                "incidents": len(labels),
                "arms": summary,
                "baseline": baseline,
                "gain": gain,
                "paired_lower_95": lower,
                "candidate_corrects": sum(c > b for _, c, b in pairs),
                "candidate_degrades": sum(c < b for _, c, b in pairs),
                "paired_corrected_incidents": [key for key, c, b in pairs if c > b],
                "paired_degraded_incidents": [key for key, c, b in pairs if c < b],
                "fallback": "B",
                "metering": "resource_only" if base_cost is None or candidate_cost is None else "cost_recorded",
                "result": result,
                "pins": {key: next(iter(values)) for key, values in pins.items()},
            },
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("run")
    evaluate.add_argument("cases")
    evaluate.add_argument("snapshot")
    evaluate.add_argument("output")
    evaluate.add_argument("--evaluator-sha", required=True)
    evaluate.add_argument("--seed", type=int, default=187)
    evaluate.add_argument("--timeout", type=int, default=10)
    scorer = sub.add_parser("score")
    scorer.add_argument("results")
    scorer.add_argument("labels")
    scorer.add_argument("--baseline", choices=("A", "B"), required=True)
    scorer.add_argument("--minimum-incidents", type=int, required=True)
    scorer.add_argument("--seed", type=int, default=187)
    worker = sub.add_parser("worker")
    worker.add_argument("arm", choices=ARMS)
    worker.add_argument("snapshot", nargs="?")
    worker.add_argument("--framed", action="store_true")
    worker.add_argument("--repetition", type=int, choices=(1, 2, 3))
    worker.add_argument("--trial-seed", type=int)
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "score":
        score(args)
    else:
        require((args.snapshot is not None) == (args.arm == "C"))

        def emit(kind, data):
            print(json.dumps({"kind": kind, "data": data}), flush=True)

        if args.framed:
            require(args.repetition is not None and args.trial_seed is not None)
            emit("result", trial(json.load(sys.stdin), args.arm, args.snapshot, emit))
        else:
            print(json.dumps(trial(json.load(sys.stdin), args.arm, args.snapshot)))


if __name__ == "__main__":
    main()
