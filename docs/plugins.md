# Strategy plugins

Plugins are trusted local Python code, like a script you run yourself. Review
code before importing it: this interface is not a Python sandbox and cannot stop
code from importing libraries, reading environment variables or opening sockets.
The provided trading interface gives a plugin **no client, wallet or signer**.
It accepts quote intents, with execution and risk managed outside the plugin.
Start in paper mode; a plugin is never loaded automatically from a remote URL.

## Try the included example

```sh
glyde-mm paper examples/plugin-paper.yaml
```

`glyde_mm/strategies/example_plugin.py` is the complete small example. To write
your own, put a Python module on your environment's import path (for example,
install your own package) and subclass `StrategyPlugin`:

```python
from dataclasses import dataclass
from decimal import Decimal
from glyde_mm.strategies.plugin import StrategyPlugin, Quote

@dataclass(frozen=True)
class Settings:
    spread: Decimal = Decimal("0.002")
    amount: Decimal = Decimal("0.0001")

    def __post_init__(self):
        if not 0 < self.spread < 1 or self.amount <= 0:
            raise ValueError("invalid spread or amount")

class MyQuotes(StrategyPlugin[Settings]):
    config_type = Settings

    def quotes(self, context, book, account):
        mid = book.mid
        if mid is None:
            return []
        return [
            Quote("buy", self.config.amount, mid * (1 - self.config.spread)),
            Quote("sell", self.config.amount, mid * (1 + self.config.spread)),
        ]
```

Select your importable class in editable YAML:

```yaml
mode: paper
strategy: plugin:my_quotes:MyQuotes
strategy_config:
  order_refresh_time: 30
  max_order_age: 300
  refresh_tolerance_pct: null
  parameters:
    spread: '0.002'
    amount: '0.0001'
```

Keep the symbol and risk settings from `examples/plugin-paper.yaml`. `parameters`
is validated against the plugin's **frozen dataclass**: supported annotations are
`Decimal`, `int`, `float`, `bool`, `str`, or one of those with `| None`. Decimal
values can be YAML decimal strings; integers/booleans/strings are not coerced into
one another. Unknown fields, missing required parameters, invalid defaults,
unsupported annotations and nonfinite numbers refuse startup. Use
`__post_init__` for strategy-specific ranges. Host execution settings above are
validated independently; plugins cannot change the configured risk limits.

## Lifecycle and execution

| Hook | Meaning |
| --- | --- |
| `async on_start(context)` | Once before the first valid book hook. |
| `async on_book(book)` | Accepted same-symbol book updates; source times preserved. |
| `async on_fill(fill)` | Fill notifications available to the runner (currently paper tape fills; live private fill hooks are not yet qualified). |
| `async on_tick(now)` | Periodic monotonic runner tick, for local state only. |
| `quotes(context, book, account)` | Synchronous complete desired quote set at the host's current execution cut. Return a list/tuple of at most 100 `Quote` objects; `[]` pulls quotes at the next refresh. |
| `async on_stop()` | After host cancellation is attempted, including error shutdown. |

Only `quotes` is required. `context` contains symbol, current price tick,
quantity step and minimum notional, without a client or global configuration.
`account.positions` is a defensive copy. Keep every hook nonblocking; async hooks
have a five-second timeout, while blocking synchronous Python can stall the
process and cannot be safely preempted by this interface.

The existing order coordinator rounds price passively and quantity down, assigns
fresh order IDs, and waits for definitive cancellation before replacement. It
calls `RiskManagedClient` for every placement; position/notional/rate/daily-loss
limits and freshness guards remain binding. An unchanged quote set can use the
normal refresh tolerance; max order age still forces replacement. Invalid
intents, plugin errors, cancellation ambiguity or unknown placement outcomes stop
the session and invoke the existing cleanup; they never trigger automatic resend.
An empty quote set is evaluated at the configured refresh, and fill notifications
force an earlier refresh. Plugins cannot directly cancel, submit, select another
symbol, set IDs or choose a taker order type through this interface.

For advanced explicit live use, follow the ordinary live startup and exposure
checks. Paper fills are simulation evidence; do not assume the same private
fill callback is available in live mode. Each configured market gets its own
plugin instance and the existing shared account risk boundary.
