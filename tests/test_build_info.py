from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

WRITER = Path(__file__).resolve().parents[1] / "scripts/write-build-info.py"


def test_embedded_build_info_contains_only_validated_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "src/glyde_mm/_build_info.json"
    target.parent.mkdir(parents=True)
    write = runpy.run_path(str(WRITER))["write_build_info"]
    write("a" * 40)
    assert json.loads(target.read_text()) == {"commit": "a" * 40}


@pytest.mark.parametrize("commit", ["secret-url", "a" * 39, "g" * 40, "A" * 40])
def test_invalid_commit_never_creates_packaged_identity(commit, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write = runpy.run_path(str(WRITER))["write_build_info"]
    with pytest.raises(ValueError, match="commit SHA"):
        write(commit)
    assert not (tmp_path / "src").exists()
