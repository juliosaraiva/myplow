import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from tools import prompt_lineage


class StaticTemplateTests(unittest.TestCase):
    def test_every_catalog_digest_round_trips_from_git(self):
        for name in json.loads(prompt_lineage.CATALOG.read_text()):
            with self.subTest(name=name):
                template, digest = prompt_lineage.static_template(name, "HEAD")
                self.assertEqual(hashlib.sha256(template.encode()).hexdigest(), digest)
        template, _ = prompt_lineage.static_template("myplow-plow-chat", "HEAD")
        self.assertIn("{their_message}", template)

    def test_extraction_rejects_dynamic_values(self):
        with self.assertRaisesRegex(ValueError, "static strings"):
            prompt_lineage.python_template("TEMPLATE = read_env()", "TEMPLATE")

    def test_catalog_loads_only_committed_bytes_matching_reviewed_digest(self):
        template, digest = prompt_lineage.static_template("myplow-boss-doctrine", "HEAD")
        self.assertEqual(hashlib.sha256(template.encode()).hexdigest(), digest)
        with self.assertRaises(KeyError):
            prompt_lineage.static_template("../runtime-user-message", "HEAD")

        with TemporaryDirectory() as temp:
            catalog = Path(temp) / "catalog.json"
            catalog.write_text(prompt_lineage.CATALOG.read_text().replace(digest, "0" * 64))
            with patch.object(prompt_lineage, "CATALOG", catalog):
                with self.assertRaisesRegex(ValueError, "digest"):
                    prompt_lineage.static_template("myplow-boss-doctrine", "HEAD")


@unittest.skipUnless(os.getenv("MYPLOW_TEST_MLFLOW"), "Requires disposable loopback MLflow")
class PromptRegistryTests(unittest.TestCase):
    def test_versions_read_back_and_runs_contain_only_provenance(self):
        from mlflow import MlflowClient

        uri = os.environ["MYPLOW_TEST_MLFLOW"]
        parsed = urlsplit(uri)
        if (parsed.scheme, parsed.netloc) != ("http", f"127.0.0.1:{parsed.port}"):
            self.fail("Integration fixture requires a disposable loopback server")
        tracking = patch.dict(os.environ, {"MLFLOW_TRACKING_URI": uri})
        tracking.start()
        self.addCleanup(tracking.stop)
        client = MlflowClient(tracking_uri=uri, registry_uri=uri)
        name = f"myplow-synthetic-{uuid4().hex}"
        experiment = client.create_experiment(name)
        versions = []
        for template, version in (
            ("Synthetic {{message}}", None),
            ("Revised {{message}}", None),
            ("Synthetic {{message}}", 1),
        ):
            run_id = client.create_run(experiment).info.run_id
            digest = hashlib.sha256(template.encode()).hexdigest()
            result = prompt_lineage.record_prompt(client, run_id, name, template, digest, version)
            self.assertEqual(result, prompt_lineage.record_prompt(client, run_id, name, template, digest))
            versions.append(result["version"])
            client.set_terminated(run_id)
            run = client.get_run(run_id)
            self.assertEqual(run.info.status, "FINISHED")
            self.assertEqual(run.data.params, {
                f"prompt.{name}.name": name,
                f"prompt.{name}.version": result["version"],
                f"prompt.{name}.sha256": digest,
            })
            self.assertFalse(run.data.metrics)
            self.assertEqual(client.list_artifacts(run_id), [])
            self.assertEqual(client.load_prompt(name, version=result["version"]).template, template)
        self.assertEqual(versions, ["1", "2", "1"])
