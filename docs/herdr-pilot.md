# Optional Herdr transport pilot

The default MyPlow transport remains tmux. Herdr pilot mode is for a disposable local MyPlow install and a private, separately named Herdr 0.9.5 server. Start it with a separate HOME/config and endpoint. Set `MYPEOPLE_HERDR_SOCKET` to that server's absolute `sessions/NAME/herdr.sock` path only for pilot commands. Never set it in the live supervisor environment.

- `mp spawn HOST/herdr-pilot:engineer --role engineer --temporary --backend codex` uses the normal role/launch path.
- `mp send --delivery-id ID HOST/herdr-pilot:engineer "task"` requires a ready pilot pane and an operator-supplied stable delivery ID.
- `mp peek HOST/herdr-pilot:engineer` reads the bound terminal.
- `mp reconnect HOST/herdr-pilot:engineer` opens a new client connection and revalidates the recorded identities. Add `--attach` with an absolute `MYPEOPLE_HERDR_BIN` for direct terminal attachment. Herdr detaches with Ctrl-B, then Q.
- `mp kill HOST/herdr-pilot:engineer` closes only the bound pane.

Transport state is separate from the tmux roster. The pilot stores endpoint identity, pane/terminal IDs and delivery digests in a private local SQLite file. It refuses absent or stale mappings and does not fall back to tmux. An ambiguous send remains fenced under the same delivery ID. Check terminal readback before manually deciding what happened. Reuse the delivery ID for a logical retry. A new delivery ID represents a new action.

Reconnect here means a new MyPlow CLI connection to the same live terminal. Server restarts, full-workspace TUI attach, fleet routing, Board ownership, worker recording, model switches and cross-host reconnect are outside the pilot. Use a local synthetic engineer for the transport test to avoid model/provider calls. A real model backend must expose an idle Herdr status before accepting input.
