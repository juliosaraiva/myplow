"""The Makefile passes the same nonempty module list to both Python orders."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class OrderedDiscoveryTests(unittest.TestCase):
    def fixture(self, root):
        shutil.copyfile(ROOT / "Makefile", root / "Makefile")
        (root / "mypeople").mkdir()
        shutil.copyfile(ROOT / "mypeople" / "__init__.py", root / "mypeople" / "__init__.py")
        tools = root / "tools"
        tools.mkdir()
        for name in ("test_modules.py",):
            source = ROOT / "tools" / name
            if source.exists():
                shutil.copyfile(source, tools / name)
        (root / "tests").mkdir()
        find = root / "bin"
        find.mkdir()
        (find / "find").write_text("#!/bin/sh\nexit 79\n")
        (find / "find").chmod(0o755)
        python = root / "python"
        python.write_text(f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:3] == ['-m', 'unittest']:
    with open({str(root / "argv.jsonl")!r}, 'a') as f:
        print(json.dumps(sys.argv[4:]), file=f)
    raise SystemExit(0)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""")
        python.chmod(0o755)
        return python

    def invoke(self, root, python):
        env = dict(os.environ, PATH=f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(["make", "test", f"PYTHON={python}"], cwd=root, env=env, capture_output=True, timeout=15)

    def test_exact_forward_and_reverse_even_without_gnu_find(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = self.fixture(root)
            for name in ("z/test_a.py", "a/test_z.py", "test_root.py"):
                path = root / "tests" / name
                path.parent.mkdir(exist_ok=True)
                path.touch()
            self.assertEqual(0, self.invoke(root, python).returncode)
            calls = [json.loads(row) for row in (root / "argv.jsonl").read_text().splitlines()]
            forward = ["a.test_z", "test_root", "z.test_a"]
            self.assertEqual([forward, list(reversed(forward))], calls)

    def test_empty_discovery_stops_before_unittest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = self.fixture(root)
            self.assertNotEqual(0, self.invoke(root, python).returncode)
            self.assertFalse((root / "argv.jsonl").exists())

    def test_failed_enumerator_stops_before_unittest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = self.fixture(root)
            (root / "tools" / "test_modules.py").write_text("print('test_partial'); raise SystemExit(9)\n")
            self.assertNotEqual(0, self.invoke(root, python).returncode)
            self.assertFalse((root / "argv.jsonl").exists())
