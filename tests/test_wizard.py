from __future__ import annotations

import stat

import pytest
import yaml

from glyde_mm.wizard import run_wizard


def test_defaults_are_paper_private_and_have_no_secret(tmp_path):
    path = run_wizard(tmp_path / "bot.yaml", interactive=False, print_fn=lambda _: None)
    data = yaml.safe_load(path.read_text())
    assert data["mode"] == "paper"
    assert data["private_key_env"] == "GLYDE_API_PRIVATE_KEY"
    assert data["strategy_config"]["hanging_orders_enabled"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private_key:" not in path.read_text()
    assert data["risk"]["max_order_notional"] == "10"


def test_interactive_validation_repompts_without_printing_input(tmp_path):
    private_looking = "0x" + "a" * 64
    answers = iter(
        [
            "bad symbol",
            "ETH",
            "NaN",
            "0.001",
            "1",
            "0.003",
            "20",
            "0.01",
            "5",
            private_looking,
            "MY_SIGNING_KEY",
        ]
    )
    printed = []
    path = run_wizard(
        tmp_path / "bot.yaml", input_fn=lambda _: next(answers), print_fn=printed.append
    )
    data = yaml.safe_load(path.read_text())
    assert data["symbol"] == "ETH"
    assert data["private_key_env"] == "MY_SIGNING_KEY"
    assert data["strategy_config"]["order_amount"] == "0.001"
    assert private_looking not in path.read_text()
    assert private_looking not in " ".join(printed)


def test_existing_file_and_symlink_are_never_overwritten(tmp_path):
    path = tmp_path / "bot.yaml"
    path.write_text("original")
    with pytest.raises(FileExistsError):
        run_wizard(path, interactive=False)
    assert path.read_text() == "original"
    link = tmp_path / "link.yaml"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        run_wizard(link, interactive=False)
    assert path.read_text() == "original"


def test_invalid_combined_limits_do_not_write_a_partial_file(tmp_path):
    answers = iter(["", "1", "", "", "0.1", "", ""])
    path = tmp_path / "bot.yaml"
    with pytest.raises(ValueError, match="exceeds"):
        run_wizard(path, input_fn=lambda _: next(answers), print_fn=lambda _: None)
    assert not path.exists()


def test_no_environment_secret_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "never-read-this-value")
    path = run_wizard(tmp_path / "bot.yaml", interactive=False, print_fn=lambda _: None)
    assert "never-read-this-value" not in path.read_text()
