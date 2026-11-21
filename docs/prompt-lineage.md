# MyPlow prompt lineage

Use a separate MLflow client environment. After reviewing a static Git blob,
record its path and SHA-256 in `tools/static_prompts.json`. Keep the existing
logical name when changing a template. The catalog covers the committed role
personalities, Boss doctrine, authentication probe and built-in plugin
templates. Python templates are extracted as static strings without importing
plugins. The catalog digest covers template bytes, including placeholders. Generated role bundles
and operator-provided overrides stay outside this catalog.

Run from a Git checkout with an existing MLflow run for the exact template
source revision.

```sh
python tools/prompt_lineage.py --tracking-uri http://127.0.0.1:5000 \
  --run-id "$RUN_ID" --revision "$SOURCE_COMMIT" myplow-role-engineer
```

The command publishes only digest-matched catalog text, reads the numeric
registry version back and logs its name, version and SHA-256 as immutable run
parameters. Specify `--version` to reuse an existing version on another run.
An initial publish creates a version. Use a fresh run for a different version. If registration succeeds but run logging fails, pass the verified numeric version on retry to avoid creating another version. Review each catalog update for sensitive content before using it.

If the target has no Prompt Registry, use a catalogued Git blob keyed by its
SHA-256 as the source of truth. An artifact-backed variant can store the same
bytes at `static-prompts/<name>/<sha256>.txt` in a dedicated synthetic run
after verifying canonical run-relative upload, list and download. Use the
digest as the immutable version and verify it over downloaded bytes. Keep the
run parameters limited to name, version and SHA-256. Legion's preserved study
reported inconsistent canonical artifact listing, so this fallback requires
separate validation before relying on its artifact copy.
