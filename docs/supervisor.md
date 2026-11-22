# MyPlow supervisor

Supervisor mode is opt-in for the existing `github-prs` plugin. Trial it with disposable board, GitHub and messaging fixtures. Live activation is a separate release decision through `make live` from a clean, approved tag. Keep plugin and board on the same trusted host. Keep the TODO UI and queue proxy on controlled loopback. Supervisor completion and linkage require both the browser session and a distinct owner credential. Protect the board process and its configuration from local accounts that can modify the installation.

| Setting | Value |
| --- | --- |
| `SUPERVISOR_ENABLED` | `1` selects supervisor mode instead of legacy GitHub polling and catch-up. |
| `SUPERVISOR_REPOS` | Space-separated GitHub repository allowlist using canonical `owner/repo` names. |
| `SUPERVISOR_REQUIRED_CHECKS` | Pipe-separated unique GitHub Actions job check names required for readiness. |
| `SUPERVISOR_APPROVERS` | Space-separated approved GitHub reviewer logins. |
| `SUPERVISOR_COPILOT_REQUEST` | Set `1` only when GitHub automatic Copilot review is disabled and the installation can request it. Default tracks existing reviews. |
| `SUPERVISOR_WEBHOOK_PORT` | Loopback receiver port, default `9937`; authorized HTTPS ingress must forward only `POST /github-webhook`. |
| `SUPERVISOR_GITHUB_TOKEN_FILE` | Owner-only credential file for authorized GitHub reads and optional review requests. Installation credentials need a separate provisioner to keep this file current. |
| `SUPERVISOR_WEBHOOK_KEY_FILE` | Owner-only shared secret file for the GitHub webhook signature. |
| `SUPERVISOR_OWNER_VERIFIER_FILE` | Owner-only file with the lowercase SHA-256 of a separate high-entropy owner password of at least 32 ASCII characters. The owner keeps the password outside the installation and uses HTTP Basic username `owner` when opening the TODO UI. |
| `SUPERVISOR_JOURNAL_KEY_FILE` | Owner-only shared key file, 32 or more ASCII characters, available to the TODO server and plugin. The queue credential is also required to read or acknowledge journal rows. |
| `SUPERVISOR_PLOW_TOKEN_FILE`, `SUPERVISOR_JULIO_CHAT` | Owner-only Plow token file and explicit private Julio chat ID. |

Subscribe to pull requests, reviews, review comments, review threads, check runs, check suites, and workflow runs. Keep the supervisor database and board backing data persistent across installation restarts. Completed cards enter the journal only after a successful owner UI commit. Journal acknowledgement follows the supervisor database commit. The supervisor reports the recorded verification flag and never infers completion from agent messages. A GitHub merge requires provider confirmation and an exact merge SHA. Copilot findings route to the active linked owner or Boss for triage. To link a card, its owner-authorized browser session sends `POST /todo/update` with `{"op":"set","id":"CARD","supervisorPRs":["https://github.com/owner/repo/pull/1"]}`. That replaces the card’s structured links. Comments, proofs, and card text cannot establish this linkage. An unconfirmed Copilot POST or conflict remains unknown until a Copilot review on the exact target SHA is observed. Reconciliation can use a historical exact-SHA review submitted after dispatch to clear the fence and permit a newer head. It never repeats the unknown request. There is no automated merge or deployment.

The readiness message gives the time of the provider snapshot. A head change, dismissed approval, or newer required check attempt can invalidate it. A final read suppresses stale queued notices. A later observation can issue a correction to a previously sent notice. Provider state can change after that read. A healthy measured five-second first-attempt target starts at durable outbox ingestion. The periodic sweep targets a separate 60-second start cadence. On restart, reconciliation reads tracked unresolved PRs directly and paginates discovery for missing events. History prior to enrollment or inaccessible due to API coverage limits is visible in status.

Use the plugin `status` command to inspect the journal cursor, sweep recency, first-attempt delay, provider gaps, blocked work and uncertain dispatches. Definitive throttling retries use backoff. A send with no confirmed response stays visible for operator reconciliation. It is not replayed automatically. Receipts prove transport acceptance. No review text, card body, credential, authentication header, or response body is written to the supervisor ledger or status.
