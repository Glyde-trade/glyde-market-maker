"""Extract exactly the current package's changelog entry for its GitHub Release."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def release_notes(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    matches = [index for index, line in enumerate(lines) if line == f"## [{version}]"]
    if len(matches) != 1:
        raise ValueError("expected exactly one changelog entry for the package version")
    start = matches[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## [")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).strip()
    if not body or body.startswith("["):
        raise ValueError("versioned changelog entry must contain release notes")
    return body + "\n"


if __name__ == "__main__":
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    sys.stdout.write(release_notes(Path("CHANGELOG.md").read_text(), version))
