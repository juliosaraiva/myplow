"""Replay, reconciliation and dispatch for the optional supervisor."""

import hashlib
import hmac
import json
import re
import random
from datetime import datetime, timezone

from supervisor_ledger import ready_proof

EVENTS = {
    "pull_request": {"opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft", "closed"},
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "pull_request_review_thread": {"resolved", "unresolved"},
    "check_run": {"created", "completed", "rerequested"},
    "check_suite": {"requested", "completed", "rerequested"},
    "workflow_run": {"requested", "in_progress", "completed"},
}


class ProviderFailure(Exception):
    def __init__(self, reason, *, delay=0, unknown=False):
        super().__init__(reason)
        self.reason = reason
        self.delay = delay
        self.unknown = unknown


def ingest_webhook(ledger, repos, secret, signature, event, delivery, raw):
    expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not signature.isascii() or not hmac.compare_digest(expected, signature):
        raise ValueError("invalid signature")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", delivery):
        raise ValueError("invalid delivery")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    repo = payload.get("repository", {}).get("full_name")
    if repo not in repos or payload.get("action") not in EVENTS.get(event, ()):
        return False
    if event.startswith("pull_request"):
        numbers = [payload.get("number") or payload.get("pull_request", {}).get("number")]
    else:
        numbers = [pr.get("number") for pr in payload.get(event, {}).get("pull_requests", [])]
    numbers = {n for n in numbers if type(n) is int and 0 < n <= 2_147_483_647}
    return ledger.track_delivery(repo, numbers, delivery)


def replay_board(ledger, source):
    events = source.events()
    if not events:
        return False
    ids = ledger.ingest_board(events)
    source.ack(ids)
    return True


def reconcile(ledger, provider, *, full=False, request_copilot=False):
    for repo, number, revision in ledger.tracked(dirty=not full):
        try:
            snapshot = provider.snapshot(repo, number)
            ledger.apply(repo, number, revision, snapshot, request_copilot=request_copilot)
        except ProviderFailure as error:
            ledger.gap(f"{repo}#{number}", error.reason, error.delay or 60)
            if error.reason == "rate_limited":
                break


def _still_current(action, snapshot):
    data = action["data"]
    kind = action["kind"]
    if kind == "ready":
        return ready_proof(snapshot) == data["proof"]
    if snapshot["head"] != data["head"] or not snapshot["open"]:
        return False
    if kind == "review":
        return data["thread"] in snapshot["threads"]
    if kind == "copilot_request":
        return not snapshot["draft"] and snapshot["copilot"] == "none"
    return True


def dispatch_one(ledger, provider, transport, *, request_copilot=False, lane=None):
    action = ledger.claim(lane)
    if action is None:
        return False
    data, kind = action["data"], action["kind"]
    revision = None
    snapshot_at = None
    if kind == "copilot_request" and not request_copilot:
        ledger.finish(action, "cancelled", reason="request_mode_disabled")
        return True
    if kind in ("ready", "review", "copilot_request", "blocked_notice"):
        repo, number = data["repo"], data["number"]
        revision = ledger.revision(repo, number)
        try:
            snapshot = provider.snapshot(repo, number)
        except ProviderFailure as error:
            ledger.gap(f"{repo}#{number}", error.reason, error.delay or 60)
            if kind != "blocked_notice":
                ledger.finish(action, "pending", reason=error.reason, delay=error.delay or 60)
                return True
            revision = None
        else:
            snapshot_at = datetime.fromtimestamp(ledger.clock(), timezone.utc).isoformat()
            if not ledger.apply(repo, number, revision, snapshot, request_copilot=request_copilot):
                ledger.finish(action, "pending", reason="changed_before_dispatch", delay=1)
                return True
            revision += 1
            if not _still_current(action, snapshot):
                ledger.finish(action, "cancelled", reason="superseded")
                return True
    if not ledger.begin(action, revision=revision):
        ledger.finish(action, "pending", reason="changed_before_dispatch", delay=1)
        return True
    try:
        if kind == "copilot_request":
            receipt = provider.request_review(data["repo"], data["number"])
            try:
                after = provider.snapshot(data["repo"], data["number"])
            except ProviderFailure:
                raise ProviderFailure("copilot_request_readback_unavailable", unknown=True) from None
            review = after.get("copilot_review")
            if after["head"] != data["head"] or not review:
                raise ProviderFailure("copilot_request_head_unconfirmed", unknown=True)
            receipt = f"{receipt}:review:{review}"
        else:
            receipt = transport.deliver(kind, data, snapshot_at)
    except ProviderFailure as error:
        if error.unknown:
            ledger.finish(action, "unknown", reason=error.reason)
        elif error.delay and action["attempts"] < 7:
            ledger.finish(
                action,
                "pending",
                reason=error.reason,
                delay=max(error.delay, 2 ** action["attempts"]) + random.uniform(0, 1),
            )
        else:
            ledger.finish(action, "blocked", reason=error.reason)
    else:
        ledger.finish(action, "sent", receipt=receipt)
    return True
