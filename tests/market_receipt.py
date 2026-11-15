"""Seed the public prerequisite in tests isolating later live lifecycle boundaries."""

from glyde_mm.market_preflight import _write_receipt


def approved_market(config, directory):
    if config.mode == "live":
        _write_receipt(config, directory)
