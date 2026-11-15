"""Native price constraints are cumulative, with only the advertised integer exception."""

from decimal import Decimal as D

import pytest
from order_rules_fixture import native_rules

from glyde_mm.order_rules import order_rules


@pytest.mark.parametrize(
    "price,integers,expected",
    [
        ("100000", True, True),
        ("100000", False, False),
        ("10000", False, True),
        ("10000.1", True, False),
        ("0.1", False, True),
        ("99.90", False, True),
        ("99.91", True, False),
        ("0", True, False),
        ("NaN", True, False),
    ],
)
def test_native_price_rules(price, integers, expected):
    rules = order_rules(
        {
            "szDecimals": 5,
            "glydeOrderRules": native_rules(
                integerPricesAllowed=integers,
            ),
        }
    )
    assert rules.valid_price(D(price)) is expected
