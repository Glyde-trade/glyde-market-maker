# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Hummingbot connector discovery and separate encrypted connect credentials."""

from decimal import Decimal

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema
from pydantic import ConfigDict, Field, SecretStr

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USD"
BROKER_ID = "GLYDE"
# Conservative Hummingbot budget estimate, not advertised native venue fees.
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.00025"),
    taker_percent_fee_decimal=Decimal("0.00025"),
    buy_percent_fee_deducted_from_returns=True,
)


def credential(prompt):
    return Field(
        default=...,
        json_schema_extra={
            "prompt": prompt,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )


class GlydePerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = "glyde_perpetual"
    glyde_perpetual_owner: SecretStr = credential("Registered Glyde owner address")
    glyde_perpetual_vault_address: SecretStr = credential("Assigned Glyde vault_address namespace")
    glyde_perpetual_private_key: SecretStr = credential("Approved trading wallet private key")
    glyde_perpetual_read_credential: SecretStr = credential("Separate Glyde owner read credential")
    model_config = ConfigDict(title="glyde_perpetual")


KEYS = GlydePerpetualConfigMap.model_construct()
OTHER_DOMAINS = []
