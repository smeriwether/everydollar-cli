#!/usr/bin/env python3
"""Print Homebrew `resource` blocks for this project's dependencies.

Homebrew ships `brew update-python-resources` for this, but it invokes pip with
`--uploaded-prior-to`, which pip 25.x rejects, so it fails on current setups.

Usage:
    python3 scripts/generate_resources.py [project-or-url]

The argument defaults to the repository root. Pass a release tarball URL to
generate blocks for exactly what a formula would download.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PROJECT_NAME = "everydollar-cli"


def resolve(target: str) -> list[tuple[str, str]]:
    """Return the (name, version) of every dependency pip would install."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--quiet", "--disable-pip-version-check",
                "--dry-run", "--ignore-installed",
                "--report", str(report),
                target,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(f"pip could not resolve dependencies:\n{result.stderr}")

        data = json.loads(report.read_text())

    packages = {
        (item["metadata"]["name"], item["metadata"]["version"])
        for item in data["install"]
        if item["metadata"]["name"] != PROJECT_NAME
    }
    return sorted(packages, key=lambda pair: pair[0].lower())


def sdist_for(name: str, version: str) -> dict:
    """Find the source distribution for a release.

    Homebrew installs with --no-binary=:all:, so a dependency without an sdist
    cannot be packaged.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url) as response:
        payload = json.load(response)

    for candidate in payload["urls"]:
        if candidate["packagetype"] == "sdist":
            return candidate

    sys.exit(f"{name} {version} publishes no sdist, so Homebrew cannot build it.")


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)

    for name, version in resolve(target):
        sdist = sdist_for(name, version)
        print(f'  resource "{name}" do')
        print(f'    url "{sdist["url"]}"')
        print(f'    sha256 "{sdist["digests"]["sha256"]}"')
        print("  end")
        print()


if __name__ == "__main__":
    main()
