"""Embed only a validated source commit in a package built from this checkout."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def write_build_info(commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected a 40-character lowercase commit SHA")
    Path("src/glyde_mm/_build_info.json").write_text(json.dumps({"commit": commit}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    write_build_info(parser.parse_args().commit)
