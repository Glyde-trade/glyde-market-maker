"""Minimal user-editable quote policy; execution and risk remain in the host."""

from dataclasses import dataclass
from decimal import Decimal

from glyde_mm.models import Account, Book

from .plugin import PluginContext, Quote, StrategyPlugin


@dataclass(frozen=True)
class SymmetricConfig:
    spread: Decimal = Decimal("0.002")
    amount: Decimal = Decimal("0.0001")

    def __post_init__(self) -> None:
        if not 0 < self.spread < 1 or self.amount <= 0:
            raise ValueError("spread must be in (0, 1) and amount must be positive")


class SymmetricQuotes(StrategyPlugin[SymmetricConfig]):
    config_type = SymmetricConfig

    def quotes(self, context: PluginContext, book: Book, account: Account) -> list[Quote]:
        mid = book.mid
        if mid is None:
            return []
        return [
            Quote("buy", self.config.amount, mid * (1 - self.config.spread)),
            Quote("sell", self.config.amount, mid * (1 + self.config.spread)),
        ]
