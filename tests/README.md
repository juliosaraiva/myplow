# Tests

`make test` runs every `test_*.py` in this directory as a top-level module, in
both orders, in one process. They stay flat so that command and the
`parents[1]` path back to the repo root keep working. `sandbox.py` is the
helper that loads a runtime script against a throwaway install. It is not a
test module.

Group is by what the test watches, not a package.

## Board, pages, and the graph

`test_board_design.py`, `test_board_reliability.py`, `test_board_search.py`,
`test_board_store.py`, `test_cancelled_row.py`, `test_card_thread.py`,
`test_comment_argv.py`, `test_comment_delete.py`, `test_comment_goes_to_owner.py`,
`test_fluid_width.py`, `test_graph_is_fleet_only.py`, `test_graph_stays_cheap.py`,
`test_graph_tmux_batch.py`, `test_linkify_urls.py`, `test_mobile_and_graph_frame.py`,
`test_notification_verbatim.py`, `test_proof_attach.py`, `test_relative_times_tick.py`,
`test_version_badge.py`

## Queue, roster, send, and the fleet

`test_cron.py`, `test_overload.py`, `test_ownership.py`, `test_recorders.py`,
`test_revive_follows_registry.py`, `test_roster_lock.py`, `test_roster_merge.py`,
`test_roster_prune.py`, `test_send_and_supervise.py`, `test_send_argv.py`,
`test_send_ready.py`, `test_send_transport.py`, `test_spawn_reuse.py`,
`test_status_live_model.py`, `test_ttyd_attach_viewer.py`, `test_watchdog.py`,
`test_zombie.py`

## Login, backends, and roles

`test_agent_backend_default.py`, `test_auth_gate.py`, `test_auth_state_writers.py`,
`test_backend_choice.py`, `test_backend_ready.py`, `test_backends.py`,
`test_codex_trust.py`, `test_login_required.py`, `test_roles.py`, `test_tcc.py`,
`test_tmux_chooser.py`, `test_yolo_seed.py`

## Plugins

`test_a2a.py`, `test_access_page.py`, `test_discord_agent.py`, `test_discord_chat.py`,
`test_github_prs_plugin.py`, `test_instagram_comments_plugin.py`, `test_plow_chat.py`,
`test_plugins.py`, `test_plugins_page.py`, `test_swarm_plugin.py`,
`test_voice_call_plugin.py`, `test_voice_plugin.py`, `test_web_chat_plugin.py`

## Install, package, and cloud

`test_bundle_launcher.py`, `test_cloud_image_model.py`, `test_cloud_login.py`,
`test_cloud_pool.py`, `test_cloud_tools.py`, `test_cloud_update.py`,
`test_make_released_guard.py`, `test_materialize_upgrade.py`,
`test_mp_help_and_dash_names.py`, `test_no_downgrade.py`
