"""Install a built wheel in a fresh environment and exercise its real entry point."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def check(wheel: Path) -> None:
    wheel = wheel.resolve(strict=True)
    with ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("expected one wheel metadata record")
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
        print(f"PASS: isolated wheel install; {result.stdout.strip()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    check(parser.parse_args().wheel)
