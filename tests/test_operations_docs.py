"""Keep the promised complete key reference and bounded paper examples usable."""

import re
from dataclasses import fields
from pathlib import Path

from glyde_mm.config import Config, MarketConfig, RiskConfig, load_config
from glyde_mm.strategies.avellaneda_market_making import AvellanedaConfig
from glyde_mm.strategies.factory import validate_strategy_config
from glyde_mm.strategies.grid import GridConfig
from glyde_mm.strategies.inventory_skewed import InventorySkewedConfig
from glyde_mm.strategies.pure_market_making import PureMarketMakingConfig

ROOT = Path(__file__).parents[1]


def keys(section):
    return set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))


def test_every_builtin_yaml_key_has_an_explicit_reference_row():
    text = (ROOT / "docs/strategies.md").read_text()

    def section(start, end):
        return text.split(start, 1)[1].split(end, 1)[0]

    session = section("### Session keys", "Each entry in `markets`")
    market = section("Each entry in `markets`", "### Shared risk keys")
    risk = section("### Shared risk keys", "### Execution keys")
    common = keys(section("### Execution keys", "### PMM-only keys"))
    pmm = common | keys(section("### PMM-only keys", "### Avellaneda–Stoikov keys"))
    avellaneda = common | keys(section("### Avellaneda–Stoikov keys", "### Inventory-skewed-only"))
    inventory = avellaneda | keys(section("### Inventory-skewed-only", "### Grid-only keys"))
    grid = common | keys(section("### Grid-only keys", "## Bounded paper examples"))
    for documented, model in [
        (keys(session), Config),
        (keys(market), MarketConfig),
        (keys(risk), RiskConfig),
        (pmm, PureMarketMakingConfig),
        (avellaneda, AvellanedaConfig),
        (inventory, InventorySkewedConfig),
        (grid, GridConfig),
    ]:
        assert documented == {field.name for field in fields(model)}, model.__name__


def test_all_documented_strategy_examples_are_bounded_paper_configs():
    for name in ["paper", "inventory-skewed-paper", "avellaneda-paper", "grid-paper"]:
        config = load_config(ROOT / "examples" / (name + ".yaml"))
        assert config.mode == "paper"
        assert config.risk.max_position > 0 and config.risk.max_order_notional > 0
        assert config.risk.max_daily_loss > 0
        validate_strategy_config(config.strategy, config.strategy_config)


def test_local_docker_build_is_the_current_retail_instruction():
    docker = (ROOT / "README.md").read_text().split("## Docker", 1)[1].split("## Development", 1)[0]
    assert "docker build -t glyde-mm ." in docker
    assert "docker pull ghcr.io" not in docker
    assert "anonymous pulls are unavailable" in docker
