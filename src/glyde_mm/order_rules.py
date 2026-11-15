"""Strict public native order rules; absent rules never imply permission to trade."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .transport import JsonObject


def _precision(value: Decimal) -> tuple[int, int]:
    digits = value.as_tuple().digits
    exponent = int(value.as_tuple().exponent)
    while exponent < 0 and digits[-1] == 0:
        digits, exponent = digits[:-1], exponent + 1
    return max(0, -exponent), len(digits) + max(0, exponent)


@dataclass(frozen=True)
class OrderRules:
    quantity_step: Decimal
    minimum_notional: Decimal
    price_tick: Decimal
    max_price_decimals: int
    max_price_significant_figures: int
    integer_prices_allowed: bool

    def valid_price(self, price: Decimal) -> bool:
        if not price.is_finite() or price <= 0 or price % self.price_tick:
            return False
        places, digits = _precision(price)
        return places <= self.max_price_decimals and (
            (places == 0 and self.integer_prices_allowed)
            or digits <= self.max_price_significant_figures
        )


def order_rules(market: JsonObject) -> OrderRules:
    raw = market.get("glydeOrderRules")
    if raw is None:
        raise ValueError("glydeOrderRules unavailable; native lot/notional/price rules required.")
    expected = {
        "version",
        "quantityStep",
        "minimumNotionalUsd",
        "priceTick",
        "maxPriceDecimals",
        "maxPriceSignificantFigures",
        "integerPricesAllowed",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError("glydeOrderRules must contain exactly the supported version 1 fields.")
    size = market.get("szDecimals")
    if (
        type(size) is not int
        or not 0 <= size <= 6
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or type(raw["maxPriceDecimals"]) is not int
        or not 0 <= raw["maxPriceDecimals"] <= 6 - size
        or type(raw["maxPriceSignificantFigures"]) is not int
        or not 1 <= raw["maxPriceSignificantFigures"] <= 5
        or type(raw["integerPricesAllowed"]) is not bool
    ):
        raise ValueError("glydeOrderRules version/precision fields are invalid or unsupported.")
    amounts = []
    for name in ("quantityStep", "minimumNotionalUsd", "priceTick"):
        text = raw[name]
        if not isinstance(text, str) or not 1 <= len(text) <= 100:
            raise ValueError("glydeOrderRules amounts must be exact positive decimal strings.")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise ValueError("glydeOrderRules amounts must be decimal strings.") from None
        if not amount.is_finite() or amount <= 0 or not -28 <= amount.adjusted() <= 28:
            raise ValueError("glydeOrderRules amounts must be finite positive native decimals.")
        amounts.append(amount)
    step, minimum, tick = amounts
    if _precision(step)[0] > size or minimum != Decimal(10):
        raise ValueError("glydeOrderRules lot precision or version 1 USD 10 minimum is invalid.")
    return OrderRules(
        step,
        minimum,
        tick,
        raw["maxPriceDecimals"],
        raw["maxPriceSignificantFigures"],
        raw["integerPricesAllowed"],
    )
