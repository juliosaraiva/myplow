"""Provider and delivery adapters. Exceptions expose only fixed reason codes."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.error
import urllib.request

from supervisor import ProviderFailure

GITHUB = "https://api.github.com"
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
COPILOT = {"copilot-pull-request-reviewer[bot]", "copilot-pull-request-reviewer", "copilot"}
THREADS = """
query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:50,after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated comments(first:1) { nodes {
          fullDatabaseId author { login __typename } pullRequestReview { commit { oid } }
        } } }
      }
    }
  }
}
"""


def secret(path):
    try:
        with Path(path).open(encoding="ascii") as file:
            if stat.S_IMODE(os.fstat(file.fileno()).st_mode) & 0o077:
                raise ValueError
            value = file.read(4096).strip()
            if not value or not value.isprintable() or any(c.isspace() for c in value):
                raise ValueError
            return value
    except (OSError, ValueError):
        raise ProviderFailure("missing_or_insecure_secret") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(url, headers, body=None, *, side_effect=False, copilot=False):
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(
            request, timeout=8
        ) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError
            return json.loads(raw) if raw else {}, response.headers
    except urllib.error.HTTPError as error:
        response_headers = error.headers
        limited = error.code == 429 or (
            error.code == 403
            and (response_headers.get("Retry-After") or response_headers.get("X-RateLimit-Remaining") == "0")
        )
        if limited:
            delay = response_headers.get("Retry-After", "")
            raise ProviderFailure("rate_limited", delay=int(delay) if delay.isdecimal() else 60) from None
        if copilot and error.code == 409:
            raise ProviderFailure("review_already_pending") from None
        if copilot and error.code == 422:
            try:
                reason = json.loads(error.read(32_768)).get("failure_reason")
            except (ValueError, AttributeError):
                reason = None
            raise ProviderFailure(
                "copilot_quota" if isinstance(reason, str) and "QUOTA" in reason.upper() else "copilot_ineligible"
            ) from None
        if error.code in (401, 403, 404):
            raise ProviderFailure("permission_or_visibility") from None
        if error.code >= 500:
            raise ProviderFailure("provider_unavailable", delay=60, unknown=side_effect) from None
        raise ProviderFailure("provider_rejected") from None
    except (OSError, ValueError):
        raise ProviderFailure("provider_unconfirmed", delay=60, unknown=side_effect) from None


class GitHub:
    def __init__(self, token, required, reviewers):
        self.token = token
        self.required = required
        self.reviewers = reviewers

    @property
    def headers(self):
        token = self.token() if callable(self.token) else self.token
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    def get(self, path):
        return http(GITHUB + path, self.headers)[0]

    def pages(self, path, *, key=None, limit=20):
        sep = "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            data, headers = http(f"{GITHUB}{path}{sep}per_page=100&page={page}", self.headers)
            yield from (data[key] if key else data)
            if 'rel="next"' not in headers.get("Link", ""):
                return
        raise ProviderFailure("pagination_coverage_gap")

    def discover(self, repo):
        return self.pages(f"/repos/{repo}/pulls?state=all&sort=updated&direction=desc")

    def threads(self, repo, number, head):
        owner, name = repo.split("/")
        after = None
        for _ in range(20):
            result, _ = http(
                GITHUB + "/graphql",
                self.headers,
                {"query": THREADS, "variables": {"owner": owner, "repo": name, "number": number, "after": after}},
            )
            if result.get("errors"):
                raise ProviderFailure("review_threads_unavailable")
            threads = result["data"]["repository"]["pullRequest"]["reviewThreads"]
            for thread in threads["nodes"]:
                if thread["isResolved"] or thread["isOutdated"]:
                    continue
                comments = thread["comments"]["nodes"]
                if comments:
                    comment = comments[0]
                    author = comment.get("author") or {}
                    review = comment.get("pullRequestReview") or {}
                    if (
                        author.get("__typename") == "Bot"
                        and author.get("login", "").lower() in COPILOT
                        and (review.get("commit") or {}).get("oid") == head
                        and str(comment.get("fullDatabaseId", "")).isascii()
                        and str(comment.get("fullDatabaseId", "")).isdecimal()
                    ):
                        yield int(comment["fullDatabaseId"])
            if not threads["pageInfo"]["hasNextPage"]:
                return
            after = threads["pageInfo"]["endCursor"]
        raise ProviderFailure("review_thread_coverage_gap")

    def checks(self, repo, pr, head, base):
        if not self.required:
            return []
        valid_shas = {head}
        synthetic = pr.get("merge_commit_sha")
        if synthetic and synthetic != head and SHA.fullmatch(synthetic):
            parents = self.get(f"/repos/{repo}/git/commits/{synthetic}")["parents"]
            if [p["sha"] for p in parents] == [base, head]:
                valid_shas.add(synthetic)
        candidates = {}
        for sha in valid_shas:
            for check in self.pages(f"/repos/{repo}/commits/{sha}/check-runs?filter=all", key="check_runs"):
                name = check["name"]
                if name not in self.required or (check.get("app") or {}).get("slug") != "github-actions":
                    continue
                check_id = check["id"]
                candidates[name] = max(candidates.get(name, (0, None)), (check_id, check), key=lambda item: item[0])
        proof = []
        for name in sorted(self.required):
            if name not in candidates:
                return []
            check_id, check = candidates[name]
            match = re.fullmatch(
                rf"https://github\.com/{re.escape(repo)}/actions/runs/(\d+)(?:/job/\d+)?",
                check.get("details_url") or "",
                flags=re.IGNORECASE,
            )
            if not match or check.get("conclusion") != "success":
                return []
            run_id = int(match[1])
            run = self.get(f"/repos/{repo}/actions/runs/{run_id}")
            if (
                run.get("head_sha") not in valid_shas
                or not self._run_matches_pr(run, pr, repo, head, base, synthetic, valid_shas)
                or run.get("event") != "pull_request"
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
            ):
                return []
            attempt = run["run_attempt"]
            jobs = self.pages(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs")
            if not any(
                job.get("conclusion") == "success"
                and (job.get("check_run_url") or "").endswith(f"/check-runs/{check_id}")
                for job in jobs
            ):
                return []
            proof.append([name, check_id, run_id, attempt])
        return proof

    @staticmethod
    def _run_matches_pr(run, pr, repo, head, base, synthetic, valid_shas):
        verified_merge = bool(
            synthetic and synthetic != head and synthetic in valid_shas and run.get("head_sha") == synthetic
        )
        associated = run.get("pull_requests")
        if associated is None or associated == []:
            return verified_merge
        if not isinstance(associated, list):
            return False
        target_ref = pr["base"]["ref"]
        matches = []
        for linked in associated:
            target = linked.get("base") or {}
            repo_record = target.get("repo") or {}
            provider_repo_id = repo_record.get("id")
            wanted_repo_id = (pr["base"].get("repo") or {}).get("id")
            repo_name = repo_record.get("full_name")
            same_repo = (
                provider_repo_id == wanted_repo_id
                if wanted_repo_id is not None
                else isinstance(repo_name, str) and repo_name.lower() == repo.lower()
            )
            matches.append(
                linked.get("number") == pr["number"]
                and (linked.get("head") or {}).get("sha") == head
                and target.get("sha") == base
                and target.get("ref") == target_ref
                and same_repo
            )
        return all(matches) if verified_merge else any(matches)

    def snapshot(self, repo, number):
        root = f"/repos/{repo}/pulls/{number}"
        try:
            pr = self.get(root)
            head, base = pr["head"]["sha"], pr["base"]["sha"]
            if not (SHA.fullmatch(head) and SHA.fullmatch(base)):
                raise ValueError
            merge = pr.get("merge_commit_sha") if pr.get("merged") is True else ""
            if pr.get("merged") is True and (not merge or not SHA.fullmatch(merge)):
                raise ProviderFailure("merge_sha_unavailable")
            snap = {
                "head": head,
                "base": base,
                "open": pr["state"] == "open",
                "draft": bool(pr["draft"]),
                "merged": merge,
                "approval": None,
                "checks": [],
                "threads": [],
                "copilot": "none",
                "copilot_review": None,
                "copilot_history": {},
            }
            if not snap["open"]:
                return snap
            # These are a provider snapshot. Read PR again to reject an observed changing head.
            direct = list(self.pages(root + "/reviews"))
            states = {}
            for review in sorted(direct, key=lambda row: row["id"]):
                author = review.get("user") or {}
                login = author.get("login", "").lower()
                review_head = review.get("commit_id") or ""
                if author.get("type") == "Bot" and login in COPILOT and SHA.fullmatch(review_head):
                    submitted = review.get("submitted_at")
                    if submitted and review["state"] != "PENDING" and type(review["id"]) is int:
                        at = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                        if at.tzinfo is None:
                            raise ValueError
                        proof = [at.timestamp(), review["id"]]
                        previous = snap["copilot_history"].get(review_head)
                        if previous is None or proof > previous:
                            snap["copilot_history"][review_head] = proof
                    if review_head == head:
                        body = (review.get("body") or "").lower()
                        unavailable = "unable to review" in body or "could not review" in body
                        snap["copilot"] = "blocked" if unavailable else "reviewed"
                        if review["state"] not in ("DISMISSED", "PENDING"):
                            snap["copilot_review"] = review["id"]
                if login in self.reviewers and review["state"] in {"APPROVED", "DISMISSED", "CHANGES_REQUESTED"}:
                    states[login] = review
            pending = pr.get("requested_reviewers", [])
            if snap["copilot"] == "none" and any(
                user.get("type") == "Bot" and user.get("login", "").lower() in COPILOT for user in pending
            ):
                snap["copilot"] = "pending"
            approvals = [r["id"] for r in states.values() if r["state"] == "APPROVED" and r["commit_id"] == head]
            snap["approval"] = sorted(approvals) or None
            snap["threads"] = list(self.threads(repo, number, head))
            if snap["approval"] and not snap["draft"] and not snap["threads"]:
                snap["checks"] = self.checks(repo, pr, head, base)
            final = self.get(root)
            if any(final.get(field) != pr.get(field) for field in ("head", "base", "state", "draft", "merged")):
                raise ProviderFailure("pr_changed_during_snapshot", delay=1)
            return snap
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ProviderFailure("invalid_provider_snapshot") from None

    def request_review(self, repo, number):
        try:
            http(
                f"{GITHUB}/repos/{repo}/pulls/{number}/copilot-code-review",
                self.headers,
                {},
                side_effect=True,
                copilot=True,
            )
        except ProviderFailure as error:
            if error.reason != "review_already_pending":
                raise
            return "already_pending"
        return "request_accepted"


class Board:
    def __init__(self, port, queue_key, journal_key):
        self.root = f"http://127.0.0.1:{port}/todo"
        self.headers = {"X-Queue-Secret": queue_key, "X-MyPlow-Journal": journal_key}

    def events(self):
        return http(self.root + "/supervisor/events", self.headers)[0]["events"]

    def ack(self, ids):
        http(self.root + "/supervisor/ack", self.headers, {"ids": ids})

    def read(self):
        return http(self.root + "/board", self.headers)[0]


class Transport:
    def __init__(self, board, owner_chat, token, boss, launcher, timeout):
        self.board, self.chat, self.token = board, owner_chat, token
        self.boss, self.launcher, self.timeout = boss, launcher, timeout

    def deliver(self, kind, data, snapshot_at):
        if kind == "todo_done":
            level = "verified" if data["verified"] else "verification unrecorded"
            message = f"TODO {data['card']} moved to done ({level})."
        else:
            url = f"https://github.com/{data['repo']}/pull/{data['number']}"
            if kind.startswith("pr_merged"):
                message = f"{url} merged at {data['merge']}."
            elif kind == "ready":
                message = f"{url} readiness snapshot at {snapshot_at} for {data['head']}: current review and CI passed."
            elif kind == "correction":
                superseded_at = datetime.fromtimestamp(data["superseded_at"], timezone.utc).isoformat()
                message = f"{url} superseded readiness for {data['head']} as of {superseded_at}. Recheck before acting."
            elif kind == "blocked_notice":
                message = (
                    f"{url} Copilot review blocked for {data['head']} ({data['reason']}). Route to a human reviewer."
                )
            else:
                message = f"{url} Copilot feedback for {data['head']}: {url}#discussion_r{data['thread']}."
            if kind in {"pr_merged_owner", "review", "blocked_notice"}:
                return self._owner(url, message, data["number"])
        result, _ = http(
            f"https://api.plow.co/v1/chats/{self.chat}/messages",
            {"Authorization": f"Bearer {self.token}"},
            {"body": message},
            side_effect=True,
        )
        uid = result.get("uid") if isinstance(result, dict) else None
        if not uid or not re.fullmatch(r"msg_[A-Za-z0-9_-]+", uid):
            raise ProviderFailure("message_receipt_missing", unknown=True)
        return uid

    def _owner(self, url, message, number):
        try:
            board = self.board.read()
        except ProviderFailure:
            raise ProviderFailure("board_owner_unavailable", delay=15) from None
        owners = {
            task["assignee"]
            for task in board.get("tasks", {}).values()
            if task.get("assignee")
            and task.get("state") not in ("done", "cancelled")
            and isinstance(task.get("supervisorPRs"), list)
            and url in task["supervisorPRs"]
        }
        owner = next(iter(owners)) if len(owners) == 1 else self.boss
        if owner == self.boss:
            message += f" Route PR #{number} to its card owner."
        for recipient in dict.fromkeys((owner, self.boss)):
            try:
                result = subprocess.run(
                    [*self.launcher, "send", recipient, message],
                    capture_output=True,
                    timeout=self.timeout,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ProviderFailure("owner_delivery_unconfirmed", unknown=True) from None
            if result.returncode == 0:
                return "agent_transport_accepted"
            if result.returncode != 3:
                raise ProviderFailure("owner_delivery_unconfirmed", unknown=True)
        raise ProviderFailure("owner_unavailable", delay=15)
