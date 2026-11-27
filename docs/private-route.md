# Private Boss route

The private route is an opt-in HTTP/JSON service on a Unix socket. It accepts status requests, private task proposals and approval decisions. Profiles need an explicit task grant. A dedicated Codex Boss can receive tasks and publish responses using `boss serve`. The broker binds every return event to its request and to the authenticated profile’s stream.

`status` reports that the configured private route accepts requests. The task protocol records a proposal, acknowledgement, approval and result. The autonomous worker accepts two typed decisions from the model. Its supported output is a private approval question or a private result. A local operator can also handle tasks manually with the existing `boss` commands.

## Run an isolated instance

From the checkout root, start a disposable instance and request status.

```sh
fixture=$(mktemp -d /tmp/myplow-route.XXXXXX)
entry="$PWD/mypeople/runtime/plugins/private-route/private-route.py"
route() { python3 "$entry" --state-dir "$fixture/state" "$@"; }
client() { route client --credential-file "$fixture/client.cred" "$@"; }

route route on
route grant local-agent --allow-task --credential-file "$fixture/client.cred"
python3 "$entry" --state-dir "$fixture/state" serve &
broker_pid=$!
while [ ! -S "$fixture/state/route.sock" ]; do
    kill -0 "$broker_pid" 2>/dev/null || break
    sleep 0.1
done

client status status-1
client events
# Use request_id from the receipt and next_cursor from the event response.
client request REQUEST_ID
client events --cursor NEXT_CURSOR
```

## Run the autonomous Boss

Start one worker against the same private state directory. Use an absolute path to the approved Codex executable and a dedicated, already authenticated `CODEX_HOME`. Pass an isolated Herdr Core executable and socket together to run the Codex turns under Herdr.

```sh
route boss serve --codex "$pilot_codex" --codex-home "$pilot_codex_home" \
    --herdr "$private_herdr" --herdr-socket "$private_herdr_socket" &
worker_pid=$!

printf '%s' 'Summarize this synthetic example' | client task task-1
client events --cursor NEXT_CURSOR
# After receiving approval_required, use that task's request_id.
client approval REQUEST_ID approve
client events --cursor NEXT_CURSOR
route boss evidence REQUEST_ID
```

The worker discovers queued tasks and gives the first turn to the pinned Boss role. The model chooses `request-approval`. The worker records the native thread ID and publishes the approval question in one SQLite transaction. After authenticated approval it resumes that exact thread with the approval signal. The task text stays in the thread across resume. The model chooses `result`, which the worker publishes to the same request. A task owned by the worker cannot be completed by the manual CLI. `boss evidence` reports the native thread and correlated event IDs without request or response bodies.

The broker enforces one committed result event per request. A process loss during a model turn can lead to another model turn when the worker restarts. Shutdown during an approval wait leaves a durable thread checkpoint for resume. The dedicated Codex home retains its native session under Codex’s retention policy. Use only synthetic material in the pilot.

The model runs with an isolated process environment and Codex tools, hooks, apps, plugins, delegation and web search disabled. An invalid action, unexpected tool use or different native thread stops the worker with a diagnostic code. The request remains available for operator inspection and restart. Typed actions carry no destination. An optional private Herdr instance runs the fixed Codex command without opening a pane.

## Manual task example

Run this example with the autonomous worker stopped. Use the returned task request ID in the subsequent calls.

```sh
printf '%s' 'Summarize the synthetic example' | client task task-1
route boss inbox
route boss accept REQUEST_ID
printf '%s' 'Approve preparing the summary?' | route boss request-approval REQUEST_ID
client approval REQUEST_ID approve
printf '%s' 'Synthetic example summarized.' | route boss result REQUEST_ID
client events --cursor NEXT_CURSOR
```

Pass actual task and result text through stdin. A heredoc or private file keeps that text out of shell command arguments. Keep `boss inbox` and client output on the private side of the integration. The schema gives the Boss the verified profile, request ID, phase and task text. The client credential remains in its own file.

## Reproduce the acceptance tests

```sh
cd tests
PYTHONPATH=.. PRIVATE_ROUTE_TEST_HERDR="$(command -v herdr-core)" \
    python3 -m unittest -v plugins.test_private_route
```

Set `PRIVATE_ROUTE_TEST_HERDR` to the absolute path of a Herdr Core binary to exercise its real terminal-free `exec` API and restart. The tests give it a fresh `HOME`, runtime directory, config directory and `HC_SOCKET` under `/tmp`. The offline model fixture exercises autonomous approval and result, with thread, request and event correlation before and after restart. The negative test verifies rejection of a different thread and manual publication to a worker-owned request. With the variable unset, the tests exercise local processes directly. The human positive control uses the existing Plow Chat route with a local test collector. The private client and broker run with audit hooks that detect attempts to open non-Unix sockets, connect to another socket or spawn a helper.

## HTTP and persistence

The socket and client credential file have mode `0600` and the service directory has mode `0700`. Use a short absolute socket directory on macOS. Pass the generated bearer through the `Authorization` header over the Unix socket. The shipped client reads it from the file and writes it directly to the socket. Store credentials for different client identities in separate files. Control of the service directory or the service’s OS account is a trusted local operator capability. A deployment that needs to isolate mutually untrusted same-UID processes requires an OS boundary.

| Method and path | JSON object | Result |
| --- | --- | --- |
| `POST /v1/requests` | `key` and `operation`. Add `text` for `task`. | Request ID, phase and expiry. `201` for creation and `200` for an identical retry. |
| `POST /v1/request` | `request_id` | Current receipt for a request owned by this profile. |
| `POST /v1/events` | `cursor`, initially `null` | Up to 50 events, `next_cursor` and `has_more`. Save the cursor after processing the page. |
| `POST /v1/approval` | `request_id` and `decision` of `approve` or `decline` | Updated task receipt. |

Requests, transitions and return events are transactional. Reuse the same key and payload after ambiguous delivery. A changed payload or decision returns `409`. The durable inbox can show an existing task again after a worker restart. Resume from its stored phase. Retrying a committed transition with the same content preserves its event ID. Cursor reads can return a page again when the consumer deliberately reuses an earlier cursor. Consumers can use the event ID to make processing idempotent.

A profile without the task grant can request status and inspect its own stream. A wrong profile’s request ID or cursor returns `404`. Missing, invalid and expired credentials return `401`. A stopped route returns `503` for new submissions or transitions. Existing results remain readable until request expiry. The credential and request TTLs both default to one hour. Request expiry preserves the key, ID and cursor metadata for replay rejection and cursor continuity. The broker periodically clears expired payloads. Keep the opt-in state directory on a local private filesystem and retain it across restarts.

## Pause, rotate and stop

Run `route route off` to pause new requests and transitions while preserving the state. Run `route route on` to resume. `route revoke local-agent` revokes the profile immediately and pauses work for that profile. Rotate with `grant` to a fresh credential file for the same profile and securely remove the superseded client file. For a different principal, grant a different profile. Granting the same profile deliberately preserves its private history.

Stop a foreground worker with `kill -TERM "$worker_pid"` and `wait "$worker_pid"`. Stop the broker the same way using `broker_pid`. Start both with the same state and dedicated Codex home to restore thread bindings, requests and cursors. The existing plugin supervisor starts the broker only when `MP_PRIVATE_ROUTE_STATE_DIR` is set. Use `mp plugins off private-route` to stop a supervised broker and prevent respawn. The worker has its own launch and shutdown commands.

## Native A2A evaluation

The native A2A plugin authenticates paired MyPlow hosts and passes messages through an HTTPS relay into the general queue. It routes by destination agent and conveys peer permissions as text. The private route uses its own local Unix transport, validates operations in code and keeps its return stream in SQLite. The native pairing and human channel configuration stay independent.
