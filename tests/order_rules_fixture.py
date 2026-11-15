"""Illustrative producer contract, never inferred trading authority."""


def native_rules(**changes):
    return {
        "version": 1,
        "quantityStep": "0.00002",
        "minimumNotionalUsd": "10",
        "priceTick": "0.1",
        "maxPriceDecimals": 1,
        "maxPriceSignificantFigures": 5,
        "integerPricesAllowed": True,
        **changes,
    }
