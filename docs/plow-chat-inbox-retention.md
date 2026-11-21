# MyPlow inbound inbox retention

Inbound messages stay in the private durable inbox while handoff is unresolved. Refusal,
transport timeout, process death, or missing pane acknowledgment never silently discards
the row or its payload. Once the handoff is accepted, the inbox clears `message_json` and
keeps only the delivery tombstone (IDs, ordering, state, and timestamps).

At 24 hours unresolved rows generate a redacted alert to the Boss with chat/message IDs,
state, and an explicit resolution command. No message body, filename, or attachment bytes
go in that alert. Resolve by retrying or explicitly discarding:

```sh
python3 mypeople/runtime/plugins/plow-chat/plow-chat.py resolve-pending CHAT_UID MESSAGE_UID retry
python3 mypeople/runtime/plugins/plow-chat/plow-chat.py resolve-pending CHAT_UID MESSAGE_UID discard
```

Retry retains the durable payload. Discard deletes the pending database row and its
associated downloaded attachment files. Both accepted handoff and discard write a cleanup
job in the same transaction that clears the payload reference. Cleanup replays on database
open; unlink is idempotent, and an `OSError` leaves the job queued. Jobs contain only
validated child filenames. Downloaded files live in a private 0700 directory as 0600 files
while pending.

Schema v4 adds a per-message attachment manifest transactionally. New filenames derive
from a SHA-256 digest of chat UID, message UID, and attachment UID, so sanitized identifiers
cannot alias. Cleanup uses manifest rows only; it never scans a UID prefix. During migration,
v3 prefix-derived cleanup jobs are discarded as untrusted. Legacy flat files are adopted only
when their exact filename has one reference across retained inbox payloads and the file passes
private-file checks. Ambiguous files and files referenced by erased accepted tombstones remain
orphaned, with no deletion attempt; this favors preserving user data over speculative cleanup.
Ambiguity emits a redacted log alert without attachment names or contents.

Schema v4 adds the manifest and cleanup table transactionally:

```sql
BEGIN IMMEDIATE;
CREATE TABLE attachment_cleanup (path TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE attachment_manifest (
  chat_uid TEXT NOT NULL, message_uid TEXT NOT NULL, attachment_uid TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE, PRIMARY KEY(chat_uid,message_uid,attachment_uid)
);
PRAGMA user_version=4;
COMMIT;
```

Code validates the known prior schema before applying this migration; SQLite rollback
restores it if any migration step fails. Rollback from v4 requires resolving/draining cleanup
jobs and retaining manifest state before dropping both v4 tables. Do not downgrade by dropping
the manifest while v4-managed attachments still exist.

The previous v3 rollback procedure was:

```sql
BEGIN IMMEDIATE;
DROP TABLE attachment_cleanup;
PRAGMA user_version=2;
COMMIT;
```

The database enables SQLite `secure_delete` and attempts `wal_checkpoint(TRUNCATE)` after
accepted handoff. Checkpoint is best effort: another open reader can prevent truncation, and
filesystem snapshots, backups, storage remapping, or other SQLite connections can retain
historical bytes. This is not a forensic erasure guarantee. Delivery remains at-least-once:
the pane transport has no receiver-side deduplication receipt, so a crash after pane acceptance
but before durable acknowledgment can replay the same delivery ID and duplicate the handoff.
