# Verifiable notification learning

This opt-in evidence store does not change supervisor decisions or send notifications. It keeps a separate SQLite ledger under the board install's `state/learning`. Enable only on a controlled IPv4 loopback board with a private `LEARNING_GRANTS_FILE`. Do not publish the board or its queue proxy to untrusted callers. Install and configuration files are in the board operator's trust boundary. Processes sharing the board operating-system identity need separate operating-system isolation to resist direct disk access.

Use mode-0600 grant JSON keyed by SHA-256 of independently generated 64-character lowercase hex bearer tokens. Each entry has `origin` and `scopes`, for example `{"origin":"worker","scopes":[["org/repo","engineer"]]}`. Provision each worker's token in its own mode-0600 file identified by `MYPEOPLE_LEARNING_TOKEN_FILE`. Keep the board machine credential separate. A role or origin typed into a request never grants access. The trusted provider or owner supplies minimal evidence references. The ledger records authenticated origin class and does not equate worker claims with provider verification.

Workers use `mp learning < request.json` through the board's normal machine-authenticated API. The request envelope contains exactly `op`, `repo`, `role`, and `data`. Commands are:

| Operation | Data fields | Authorized principal |
| --- | --- | --- |
| record | incident, event, observed, decision, outcome, evidence | owner, provider, worker |
| propose | lesson, incident, event, condition, action, exceptions, valid_from, expires | owner, worker |
| transition | lesson, version, state | evaluator for evaluation/rejection, owner for shadow/activation/revocation |
| lookup | event, observed, exceptions | owner, evaluator, worker |
| erase | incident | owner |

IDs are randomly assigned 32-character lowercase hex identifiers, never hashes of message bodies. Times are integer UTC epoch seconds. Observed states and exception flags are `confirmed`, `unknown`, `duplicate`, `closed`, or `active`. Actions are `silence`, `notify_owner`, `route_worker` or `abstain`. Event classes are `periodic`, `merge`, `ci_failure`, `review` and `direct_request`. Outcomes are `confirmed`, `rejected`, `unknown` or `not_attempted`. The evidence ID references a separately protected observation. Keep raw text, personal IDs, rendered prompts and credentials out of this ledger and evaluation artifacts.

An evaluator can mark a draft evaluated but cannot activate it. Owner activation retires the previous active version in the same transaction. Lookup applies exact scope, current lifecycle, expiry and exceptions before returning any lesson. An explicit owner proposal backed by an owner record takes precedence. Equal-authority disagreement returns a conflict and no action. Readback includes version and digest references for the caller's provenance. Immediate human preferences continue through the existing policy channel.

Expiry prunes episode records on the next successful ledger operation. Owner erasure removes the episode, its lessons and transition history. Only the opaque lesson lineage counter and scope remain to prevent version reuse. SQLite secure deletion applies within this pilot database. Operators must also apply retention to backups. This feature emits no MLflow data or external requests.
