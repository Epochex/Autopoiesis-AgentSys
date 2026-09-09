#!/usr/bin/env python3
"""Run the backend test profile appropriate to the delivery stage."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORE_MANIFEST = REPOSITORY_ROOT / "ci" / "test_suites" / "core_business.txt"


def _manifest_targets(path: Path) -> list[str]:
    targets: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            targets.append(line)
    if not targets:
        raise RuntimeError(f"test manifest has no targets: {path}")
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fast core business path during development or the complete "
            "backend regression before a release. Extra arguments are passed to pytest."
        )
    )
    parser.add_argument("profile", choices=("core", "release"))
    args, pytest_args = parser.parse_known_args()

    if args.profile == "core":
        targets = _manifest_targets(CORE_MANIFEST)
    else:
        targets = ["tests_py"]

    if not pytest_args:
        pytest_args = ["-q"]

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *pytest_args,
        *targets,
    ]
    print(
        f"backend test profile={args.profile} targets={len(targets)}",
        flush=True,
    )
    return subprocess.run(command, cwd=REPOSITORY_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
