"""Founder presets stay loadable live configs with distinct logic and sane caps."""

from pathlib import Path

import pytest

from glyde_mm.config import load_config
from glyde_mm.strategies.factory import validate_strategy_config

ROOT = Path(__file__).parents[1]
PRESETS = sorted((ROOT / "configs/founders").glob("*.yaml"))
OWNER = "0x" + "1" * 40
VAULT = "0x" + "2" * 40


def materialize(path: Path, tmp_path: Path) -> Path:
    text = (
        path.read_text()
        .replace("REPLACE_WITH_YOUR_PUBLIC_GLYDE_ACCOUNT_ADDRESS", OWNER)
        .replace("REPLACE_WITH_YOUR_ASSIGNED_PUBLIC_VAULT_ADDRESS", VAULT)
        .replace("REPLACE_LOWER_PRICE", "57000")
        .replace("REPLACE_UPPER_PRICE", "61000")
    )
    out = tmp_path / path.name
    out.write_text(text)
    return out


def test_six_presets_ship():
    assert [p.stem for p in PRESETS] == [
        "adaptive-vol",
        "deep-book",
        "grid-wall",
        "hl-anchored",
        "inventory-shy",
        "tight-ladder",
    ]


@pytest.mark.parametrize("path", PRESETS, ids=lambda p: p.stem)
def test_preset_is_a_bounded_live_btc_config(path, tmp_path):
    text = path.read_text()
    assert text.startswith(f"# Founder preset: {path.stem}\n")
    assert "private_key" not in text.replace("private_key_env", "")
    config = load_config(materialize(path, tmp_path))
    assert config.mode == "live" and config.symbol == "BTC"
    assert config.private_key_env == "GLYDE_API_PRIVATE_KEY"
    risk = config.risk
    assert risk.max_position > 0 and risk.max_order_notional > 0 and risk.max_daily_loss > 0
    validate_strategy_config(config.strategy, config.strategy_config)


def test_presets_use_varied_logic():
    signatures = set()
    for path in PRESETS:
        text = path.read_text()
        strategy = next(line for line in text.splitlines() if line.startswith("strategy:"))
        reference = "reference_source: hyperliquid" in text
        signatures.add((strategy, reference))
    assert len(signatures) >= 5
