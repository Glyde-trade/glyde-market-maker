import json

import pytest

from glyde_mm import cli
from glyde_mm.report import summarize
from glyde_mm.runlog import RunLog


def write_run(tmp_path, *, end=True, rotate=False):
    with RunLog(tmp_path / "logs", mode="paper", strategy="grid", max_bytes=1024, backups=2) as log:
        for index in range(50 if rotate else 1):
            log.emit("progress", book_messages=index, pending_orders=2, paper_fills=0)
        if end:
            log.emit("run_ended", outcome="stopped")
        return log.directory


def test_report_summarizes_real_rotated_logs_and_marks_missing_history(tmp_path):
    directory = write_run(tmp_path, rotate=True)
    report = summarize(directory.parent)
    assert report == summarize(directory)
    row = report["runs"][0]
    assert row["outcome"] == "stopped"
    assert row["history_truncated"] is True
    assert row["segments"] == 3
    assert row["last_progress"]["book_messages"] == 49
    assert row["mode"] == "paper" and row["strategy"] == "grid"
    assert str(tmp_path) not in json.dumps(report)


def test_report_never_infers_clean_shutdown_without_exit_record(tmp_path):
    directory = write_run(tmp_path, end=False)
    assert summarize(directory)["runs"][0]["outcome"] == "incomplete"


@pytest.mark.parametrize("mutation", ["token", "message", "version", "sequence", "missing_header"])
def test_malformed_sensitive_records_fail_without_echoing_contents(tmp_path, capsys, mutation):
    directory = write_run(tmp_path)
    path = directory / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    secret = "NEVER_PRINT_THIS_CREDENTIAL"
    if mutation in ("token", "message"):
        rows[1][mutation] = secret
    elif mutation == "version":
        rows[0]["version"] = "https://user:" + secret + "@host"
    elif mutation == "sequence":
        rows[1]["sequence"] = rows[0]["sequence"]
    else:
        rows = rows[1:]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert cli.main(["report", str(directory)]) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert output.out == ""


def test_report_rejects_symlinked_segments_and_directories(tmp_path):
    directory = write_run(tmp_path)
    segment = directory / "events.jsonl"
    outside = tmp_path / "outside"
    segment.rename(outside)
    segment.symlink_to(outside)
    with pytest.raises(OSError):
        summarize(directory)
    link = tmp_path / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError):
        summarize(link)


def test_report_bounds_input_and_requires_some_logs(tmp_path):
    with pytest.raises(ValueError, match="No diagnostic"):
        summarize(tmp_path)
    directory = write_run(tmp_path)
    with (directory / "events.jsonl").open("ab") as file:
        file.truncate(1024 * 1024 + 1)
    with pytest.raises(ValueError, match="oversized"):
        summarize(directory)


def test_cli_report_never_loads_config_or_calls_runner(tmp_path, monkeypatch, capsys):
    directory = write_run(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("report must be offline")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "run_strategy", forbidden)
    assert cli.main(["report", str(directory.parent)]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["outcome"] == "stopped"


def test_report_rejects_run_identity_mismatch_and_records_after_exit(tmp_path):
    directory = write_run(tmp_path)
    path = directory / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["run_id"] = "0" * 32
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError, match="identity"):
        summarize(directory)
    rows[1]["run_id"] = directory.name[4:]
    extra = {**rows[1], "sequence": rows[-1]["sequence"] + 1}
    path.write_text("\n".join(json.dumps(row) for row in [*rows, extra]) + "\n")
    with pytest.raises(ValueError, match="follow an exit"):
        summarize(directory)
