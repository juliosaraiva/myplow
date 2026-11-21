"""Print the deterministic module list used by the two Makefile test passes."""

import os
from pathlib import Path
import sys


def raise_walk_error(error):
    raise error


def main():
    modules = []
    for directory, _, files in os.walk(".", onerror=raise_walk_error):
        for file in files:
            if file.startswith("test_") and file.endswith(".py"):
                parts = Path(directory, file).with_suffix("").parts
                if not all(part.isidentifier() for part in parts):
                    raise ValueError(f"Invalid test module path: {Path(directory, file)}")
                modules.append(".".join(parts))
    if not modules:
        raise ValueError("No test modules found")
    print(" ".join(sorted(modules)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(str(error))
