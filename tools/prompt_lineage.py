"""Register reviewed Git templates and record immutable MLflow run provenance."""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tools" / "static_prompts.json"


def python_template(source, symbol):
    """Resolve module-level string constants without importing runtime code."""
    constants = {}

    def resolve(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return resolve(node.left) + resolve(node.right)
        raise ValueError("Template must consist of static strings")

    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == symbol:
                return resolve(statement.value)
            try:
                constants[target.id] = resolve(statement.value)
            except ValueError:
                constants.pop(target.id, None)
    raise ValueError("Static template symbol is missing")


def static_template(name, revision):
    """Only a catalogued blob with its reviewed digest can reach MLflow."""
    source = json.loads(CATALOG.read_text(encoding="utf-8"))[name]
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
        cwd=ROOT,
        text=True,
    ).strip()
    body = subprocess.check_output(
        ["git", "cat-file", "blob", f"{commit}:{source['path']}"],
        cwd=ROOT,
    )
    if "symbol" in source:
        body = python_template(body, source["symbol"]).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    if digest != source["sha256"]:
        raise ValueError("Static template digest differs from reviewed catalog")
    return body.decode("utf-8"), digest


def record_prompt(client, run_id, name, template, digest, version=None):
    """Reuse matching versions and verify exact readback before logging params."""
    from mlflow.entities import Param

    if not re.fullmatch(r"myplow-[a-z0-9-]+", name):
        raise ValueError("Invalid MyPlow prompt name")
    if hashlib.sha256(template.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Prompt provenance digest mismatch")
    prefix = f"prompt.{name}."
    params = client.get_run(run_id).data.params
    recorded_version = params.get(prefix + "version")
    if recorded_version is not None and (
        params.get(prefix + "sha256") != digest or (version is not None and str(version) != recorded_version)
    ):
        raise ValueError("Run already records a different prompt version")
    version = recorded_version if recorded_version is not None else version
    if version is None:
        version = client.register_prompt(name, template).version
    version = str(version)
    pinned = client.load_prompt(name, version=version)
    if pinned.name != name or str(pinned.version) != version or pinned.template != template:
        raise ValueError("Prompt Registry exact version readback mismatch")

    provenance = {"name": name, "version": version, "sha256": digest}
    client.log_batch(
        run_id,
        params=[Param(prefix + k, v) for k, v in provenance.items()],
        synchronous=True,
    )
    return provenance


def main():
    from mlflow import MlflowClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", type=int, help="Reuse an existing numeric registry version")
    parser.add_argument("name")
    args = parser.parse_args()
    template, digest = static_template(args.name, args.revision)
    client = MlflowClient(tracking_uri=args.tracking_uri, registry_uri=args.tracking_uri)
    print(json.dumps(record_prompt(client, args.run_id, args.name, template, digest, args.version)))


if __name__ == "__main__":
    main()
