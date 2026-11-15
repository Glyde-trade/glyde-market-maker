import json
import stat
from pathlib import Path

import pytest

from glyde_mm.runlog import RunLog
from glyde_mm.version import build_identity


def test_private_rotated_segments_retain_public_header(tmp_path):
    with RunLog(tmp_path / "logs", mode="paper", strategy="grid", max_bytes=1024, backups=2) as log:
        for index in range(50):
            log.emit("progress", book_messages=index, pending_orders=2, paper_fills=0)
        log.emit("run_ended", outcome="stopped")
        directory = log.directory
    files = list(directory.glob("*.jsonl"))
    assert len(files) == 3
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_size <= 1024
        header = json.loads(path.read_text().splitlines()[0])
        assert header["event"] == "run_header"
        assert set(header) == {
            "schema",
            "run_id",
            "sequence",
            "time",
            "event",
            "version",
            "commit",
            "mode",
            "strategy",
        }
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_arbitrary_sensitive_fields_and_messages_cannot_be_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "never-log-this-secret")
    with RunLog(tmp_path / "logs", mode="paper", strategy="untrusted-secret") as log:
        for event, values in [
            ("http", {"body": "secret"}),
            ("progress", {"token": "secret"}),
            ("run_ended", {"outcome": "secret"}),
            ("progress", {"book_messages": "secret", "pending_orders": 0, "paper_fills": 0}),
        ]:
            with pytest.raises(ValueError):
                log.emit(event, **values)
        log.emit("run_ended", outcome="completed")
        path = log.path
    text = path.read_text()
    assert "secret" not in text
    assert "untrusted" not in text


def test_log_directory_cannot_be_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        RunLog(link, mode="paper", strategy="grid")
    assert list(target.iterdir()) == []


def test_build_identity_does_not_include_install_urls_or_paths():
    identity = build_identity()
    assert set(identity) == {"version", "commit"}
    assert identity["version"]
    assert identity["commit"] == "unknown" or len(identity["commit"]) == 40
    assert str(Path.home()) not in json.dumps(identity)
