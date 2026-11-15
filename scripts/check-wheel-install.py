"""Install a built wheel in a fresh environment and exercise its real entry point."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def check(wheel: Path, expected_commit: str | None = None) -> None:
    if expected_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("invalid expected source commit")
    wheel = wheel.resolve(strict=True)
    with ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("expected one wheel metadata record")
        if "glyde_mm/py.typed" not in archive.namelist():
            raise ValueError("wheel is missing the PEP 561 typing marker")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    if metadata["Name"] != "glyde-market-maker" or not metadata["Version"]:
        raise ValueError("unexpected wheel metadata")
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "GLYDE_API_PRIVATE_KEY", "GLYDE_READ_API_KEY"):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="glyde-mm-wheel-") as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=False).create(root / "venv")
        binaries = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = binaries / ("python.exe" if os.name == "nt" else "python")
        command = binaries / ("glyde-mm.exe" if os.name == "nt" else "glyde-mm")
        subprocess.run(
            [sys.executable, "-m", "pip", "--python", str(python), "install", str(wheel)],
            cwd=root,
            env=environment,
            check=True,
            timeout=300,
        )
        result = subprocess.run(
            [str(command), "--version"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.stdout.strip() != f"glyde-mm {metadata['Version']}" or result.stderr:
            raise AssertionError("installed console version does not match wheel metadata")
        if expected_commit is not None:
            identity = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import json; from glyde_mm.version import build_identity; "
                    "print(json.dumps(build_identity()))",
                ],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if json.loads(identity.stdout)["commit"] != expected_commit:
                raise AssertionError("installed build identity does not match the source commit")
        print(f"PASS: isolated wheel install; {result.stdout.strip()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    check(args.wheel, args.expected_commit)
