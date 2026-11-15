"""Risk-managed, exact-decimal market making strategies."""

from .avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .factory import create_strategy
from .pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
)

__all__ = [
    "AvellanedaConfig",
    "AvellanedaMarketMakingStrategy",
    "PureMarketMakingConfig",
    "PureMarketMakingStrategy",
    "StrategyContext",
    "create_strategy",
]
