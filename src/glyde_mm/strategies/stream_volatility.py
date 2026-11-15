"""Source-time volatility from complete book/print intervals, without synthetic samples."""

from collections import deque
from decimal import Decimal, localcontext

from ..models import Book, Trade
from ..risk import RiskViolation

ZERO = Decimal(0)


class BookTradeVolatility:
    def __init__(self, samples: int, interval_ms: int) -> None:
        self.samples = samples
        self.interval_ms = interval_ms
        self.prices: deque[Decimal] = deque(maxlen=samples + 1)
        self.bucket: int | None = None
        self.mid: Decimal | None = None
        self.notional = ZERO
        self.quantity = ZERO
        self.trades: dict[str, Trade] = {}

    def clear(self) -> None:
        self.prices.clear()
        self.bucket = None
        self._reset_interval()

    def _reset_interval(self) -> None:
        self.mid = None
        self.notional = self.quantity = ZERO
        self.trades.clear()

    def _advance(self, timestamp_ms: int) -> bool:
        bucket = timestamp_ms // self.interval_ms
        if self.bucket is not None and bucket < self.bucket:
            return False
        if self.bucket is not None and bucket > self.bucket:
            if self.mid is not None and self.quantity > ZERO:
                self.prices.append((self.mid + self.notional / self.quantity) / 2)
            else:
                self.prices.clear()
            if bucket != self.bucket + 1:
                self.prices.clear()
            self._reset_interval()
        self.bucket = bucket
        return True

    def on_book(self, book: Book) -> None:
        if self._advance(book.timestamp_ms):
            self.mid = book.mid

    def on_trade(self, trade: Trade) -> None:
        if not self._advance(trade.timestamp_ms):
            return
        previous = self.trades.get(trade.trade_id)
        if previous is not None:
            if previous != trade:
                raise RiskViolation("volatility_trade_conflict")
            return
        if len(self.trades) >= 4096:
            raise RiskViolation("volatility_trade_budget")
        self.trades[trade.trade_id] = trade
        self.notional += trade.price * trade.quantity
        self.quantity += trade.quantity

    @property
    def value(self) -> Decimal | None:
        if len(self.prices) != self.samples + 1:
            return None
        prices = tuple(self.prices)
        with localcontext() as context:
            context.prec = 50
            variance = (
                sum(((b - a) ** 2 for a, b in zip(prices[:-1], prices[1:], strict=True)), ZERO)
                / self.samples
            )
            value = variance.sqrt()
        return value if value > ZERO else None
