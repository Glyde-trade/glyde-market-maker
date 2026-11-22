"""Public configuration; credentials are referenced by environment variable name only."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
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
    max_total_notional: Decimal | None = None
    # A stale book pauses its market; past this many seconds the run stops instead.
    max_stale_pause_seconds: Decimal = Decimal("300")


@dataclass(frozen=True)
class MarketConfig:
    symbol: str
    max_position: Decimal
    strategy_config: dict[str, Any] = field(default_factory=dict)
    strategy: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol or len(self.symbol) > 64:
            raise ValueError("invalid market symbol")
        if (
            not isinstance(self.max_position, Decimal)
            or not self.max_position.is_finite()
            or self.max_position <= 0
        ):
            raise ValueError("market max_position must be positive")
        if not isinstance(self.strategy_config, dict):
            raise ValueError("market strategy_config must be a mapping")
        if self.strategy is not None and (not isinstance(self.strategy, str) or not self.strategy):
            raise ValueError("market strategy must be a nonempty name")


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    symbol: str = "BTC"
    markets: tuple[MarketConfig, ...] = ()
    base_url: str = DEFAULT_BASE_URL
    owner: str = ""
    vault_address: str | None = None
    mainnet: bool = True
    private_key_env: str = "GLYDE_API_PRIVATE_KEY"
    read_api_key_env: str = "GLYDE_READ_API_KEY"
    paper_balance: Decimal = Decimal("10000")
    strategy: str = "pure_market_making"
    strategy_config: dict[str, Any] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    metrics_enabled: bool = False
    metrics_port: int = 9464
    alerts_enabled: bool = False
    alert_webhook_env: str = "GLYDE_MM_ALERT_WEBHOOK"

    def __post_init__(self) -> None:
        if type(self.metrics_enabled) is not bool:
            raise ValueError("metrics_enabled must be boolean")
        if type(self.metrics_port) is not int or not 0 <= self.metrics_port <= 65535:
            raise ValueError("metrics_port must be an integer in 0..65535")
        if type(self.alerts_enabled) is not bool:
            raise ValueError("alerts_enabled must be boolean")
        if not isinstance(self.alert_webhook_env, str) or not re.fullmatch(
            r"[A-Z_][A-Z_0-9]{0,127}", self.alert_webhook_env
        ):
            raise ValueError("alert_webhook_env must be an environment variable name")
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
        if not isinstance(self.symbol, str) or not self.symbol or len(self.symbol) > 64:
            raise ValueError("invalid symbol")
        if self.paper_balance <= 0 or not self.paper_balance.is_finite():
            raise ValueError("paper_balance must be positive")
        cap = self.risk.max_orders_per_minute
        if type(cap) is not int or not 1 <= cap <= 6000:
            raise ValueError("max_orders_per_minute must be an integer in 1..6000")
        for name, value in vars(self.risk).items():
            if name == "max_orders_per_minute" or (name == "max_total_notional" and value is None):
                continue
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("risk limits must be finite and positive")

        if self.markets:
            if (
                not isinstance(self.markets, tuple)
                or len(self.markets) > 16
                or any(not isinstance(m, MarketConfig) for m in self.markets)
            ):
                raise ValueError("markets must contain at most 16 market configurations")
            if len(set(self.symbols)) != len(self.markets):
                raise ValueError("duplicate market symbol")
            if self.risk.max_total_notional is None:
                raise ValueError("markets requires risk.max_total_notional")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(m.symbol for m in self.markets) if self.markets else (self.symbol,)

    def market_configs(self) -> tuple[Config, ...]:
        if not self.markets:
            return (self,)
        return tuple(
            replace(
                self,
                markets=(),
                symbol=m.symbol,
                strategy=m.strategy or self.strategy,
                strategy_config={**self.strategy_config, **m.strategy_config},
                risk=replace(self.risk, max_position=m.max_position),
            )
            for m in self.markets
        )

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
    if "markets" in raw:
        if (
            "symbol" in raw
            or not isinstance(raw["markets"], list)
            or not 1 <= len(raw["markets"]) <= 16
        ):
            raise ValueError("use markets (1..16 entries) or symbol, not both")
        markets = []
        for item in raw["markets"]:
            if (
                not isinstance(item, dict)
                or set(item) - {"symbol", "max_position", "strategy_config", "strategy"}
                or not {"symbol", "max_position"} <= set(item)
            ):
                raise ValueError("market requires symbol and max_position")
            markets.append(
                MarketConfig(
                    item["symbol"],
                    decimal(item["max_position"]),
                    item.get("strategy_config", {}),
                    item.get("strategy"),
                )
            )
        raw["markets"] = tuple(markets)
    risk = raw.pop("risk", {})
    if not isinstance(risk, dict) or set(risk) - set(RiskConfig.__dataclass_fields__):
        raise ValueError("invalid risk configuration")
    risk_values: dict[str, Any] = {
        key: value if key == "max_orders_per_minute" else decimal(value)
        for key, value in risk.items()
    }
    raw["risk"] = RiskConfig(**risk_values)
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
            if value is not None
        },
    }
    # Exclusive creation prevents overwriting an existing configuration.
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as target:
        yaml.safe_dump(template, target, sort_keys=False)
