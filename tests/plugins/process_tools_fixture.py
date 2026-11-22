"""Shadow process utilities only inside a test's environment."""

import os
from pathlib import Path
import shlex
import shutil
import sys


def shadow_tools(directory):
    root = Path(directory)
    bin_dir = root / "shadow-process-tools"
    bin_dir.mkdir()
    calls = root / "process-tool-calls"
    for name in ("pgrep", "pkill"):
        real = shutil.which(name)
        assert real
        replacement = bin_dir / name
        fallback = "exit 1" if sys.platform == "darwin" else f'exec {shlex.quote(real)} "$@"'
        replacement.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {name} >> {shlex.quote(str(calls))}\n{fallback}\n",
            encoding="utf-8",
        )
        replacement.chmod(0o700)
    return str(bin_dir) + os.pathsep + os.environ["PATH"], calls
