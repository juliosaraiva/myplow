# Private Boss route

The private route is an opt-in HTTP/JSON service on a Unix socket. It accepts a status request and, for profiles granted task access, a private task proposal and an approval decision. A local Boss operator reads task proposals and records transitions through the `boss` commands. The service writes the return events to the authenticated profile’s stream. Both the client and Boss commands use the request ID to correlate their work. The broker assigns the destination.

`status` reports that the configured private route accepts requests. Fleet status and model or pane health are outside the status payload. The task protocol records a proposal, acknowledgement, approval and result. The operator decides what result to record. The broker has no command runner or human notification adapter.

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

To exercise a task, use the returned task request ID in the subsequent calls.

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

Set `PRIVATE_ROUTE_TEST_HERDR` to the absolute path of a Herdr Core binary to exercise its real terminal-free `exec` API and restart. The tests give it a fresh `HOME`, runtime directory, config directory and `HC_SOCKET` under `/tmp`. They verify an empty terminal inventory before and after restart, resume an approved request from SQLite and read its result from the previously saved client cursor. With the variable unset, the tests exercise the same Boss commands as ordinary local processes. The human positive control exercises the existing Plow Chat route against a bound local test collector. The private client and broker run with audit hooks that record and reject attempts to open non-Unix sockets, connect to another socket or spawn a helper. No external accounts are needed.

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

Stop a manual instance with `kill -TERM "$broker_pid"` and `wait "$broker_pid"`. Start it with the same state directory to restore requests and cursors. The existing plugin supervisor starts the service only when `MP_PRIVATE_ROUTE_STATE_DIR` is set. For a supervised instance, use `mp plugins off private-route` to stop it and prevent respawn. The state directory remains available for a later opt-in launch.

## Native A2A evaluation

The native A2A plugin authenticates paired MyPlow hosts and passes messages through an HTTPS relay into the general queue. It routes by destination agent and conveys peer permissions as text. The private route uses its own local Unix transport, validates operations in code and keeps its return stream in SQLite. The native pairing and human channel configuration stay independent.
