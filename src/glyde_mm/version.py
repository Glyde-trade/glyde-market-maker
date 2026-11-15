"""Public build identity without exposing installation URLs or filesystem paths."""

from __future__ import annotations

import json
import re
import subprocess
from importlib import metadata
from pathlib import Path

from . import __version__


def build_identity() -> dict[str, str]:
    version = __version__
    try:
        distribution = metadata.distribution("glyde-market-maker")
        version = distribution.version
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        commit = direct.get("vcs_info", {}).get("commit_id", "")
        if not isinstance(commit, str):
            commit = ""
    except (metadata.PackageNotFoundError, ValueError, AttributeError):
        commit = ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        try:
            embedded = json.loads(Path(__file__).with_name("_build_info.json").read_text())
            commit = embedded.get("commit", "")
            if not isinstance(commit, str):
                commit = ""
        except (OSError, ValueError, AttributeError):
            commit = ""
    root = Path(__file__).resolve().parents[2]
    if not re.fullmatch(r"[0-9a-f]{40}", commit or "") and (root / ".git").exists():
        try:
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=1,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    return {
        "version": version if re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", version) else "unknown",
        "commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit or "") else "unknown",
    }
