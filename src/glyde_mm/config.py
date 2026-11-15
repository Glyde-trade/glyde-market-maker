"""Public configuration; credentials are referenced by environment variable name only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT_BASE_URL = "https://api.glyde.trade/v1"


def decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("expected a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("expected a finite decimal")
    return result


@dataclass(frozen=True)
class RiskConfig:
    max_position: Decimal = Decimal("0.01")
    max_order_notional: Decimal = Decimal("10")
    max_daily_loss: Decimal = Decimal("5")
    stale_book_seconds: Decimal = Decimal("5")
    account_age_seconds: Decimal = Decimal("10")
    max_orders_per_minute: int = 60


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    symbol: str = "BTC"
    base_url: str = DEFAULT_BASE_URL
    owner: str = ""
    vault_address: str | None = None
    mainnet: bool = True
    private_key_env: str = "GLYDE_API_PRIVATE_KEY"
    read_api_key_env: str = "GLYDE_READ_API_KEY"
    paper_balance: Decimal = Decimal("10000")
    risk_state_path: str = ".glyde-mm/risk-state.json"
    strategy: str = "pure_market_making"
    strategy_config: dict = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("base_url must be HTTPS (HTTP allowed only on loopback)")
        if self.mode not in {"paper", "live"}:
            raise ValueError("mode must be paper or live")
        if not self.symbol or len(self.symbol) > 64:
            raise ValueError("invalid symbol")
        if self.paper_balance <= 0 or not self.paper_balance.is_finite():
            raise ValueError("paper_balance must be positive")
        cap = self.risk.max_orders_per_minute
        if type(cap) is not int or not 1 <= cap <= 6000:
            raise ValueError("max_orders_per_minute must be an integer in 1..6000")
        for name, value in vars(self.risk).items():
            if name == "max_orders_per_minute":
                continue
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("risk limits must be finite and positive")

    def secret(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"required environment variable {name} is not set")
        return value


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError:
        raise ValueError("invalid YAML configuration") from None
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a mapping")
    if set(raw) - set(Config.__dataclass_fields__):
        raise ValueError("unknown configuration field (store keys in environment variables)")
    risk = raw.pop("risk", {})
    if not isinstance(risk, dict) or set(risk) - set(RiskConfig.__dataclass_fields__):
        raise ValueError("invalid risk configuration")
    raw["risk"] = RiskConfig(
        **{
            key: value if key == "max_orders_per_minute" else decimal(value)
            for key, value in risk.items()
        }
    )
    if "paper_balance" in raw:
        raw["paper_balance"] = decimal(raw["paper_balance"])
    if "mainnet" in raw and not isinstance(raw["mainnet"], bool):
        raise ValueError("mainnet must be a YAML boolean")
    if "strategy_config" in raw and not isinstance(raw["strategy_config"], dict):
        raise ValueError("strategy_config must be a mapping")
    return Config(**raw)


def write_config(path: str | Path) -> None:
    template = {
        "mode": "paper",
        "symbol": "BTC",
        "base_url": DEFAULT_BASE_URL,
        "strategy": "pure_market_making",
        "paper_balance": "10000",
        "private_key_env": "GLYDE_API_PRIVATE_KEY",
        "read_api_key_env": "GLYDE_READ_API_KEY",
        "risk": {
            key: value if key == "max_orders_per_minute" else str(value)
            for key, value in vars(RiskConfig()).items()
        },
    }
    # Exclusive creation prevents overwriting an existing configuration.
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as target:
        yaml.safe_dump(template, target, sort_keys=False)
