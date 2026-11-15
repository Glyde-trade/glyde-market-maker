# Streaming paper fills

`glyde-mm paper <config>` subscribes to public `l2Book` **and** `trades` on Glyde.
Quotes still pass through the strategy and risk boundary. No private subscription,
trading key or exchange submission is used. A quiet market can still have zero
fills; the simulator does not invent trades to make a report look profitable.

Each new resting quote starts behind the displayed quantity at its exact price.
A public sell print can fill paper bids; a public buy print can fill paper asks.
At a price touch, printed size first consumes the estimated queue ahead, then
fills the paper quote partially or fully. A print through the quote clears that
price's estimated queue. Fills execute at the resting limit price. Paper quotes
share one print's remaining size in price/time order, rather than each receiving
its full size independently. Same-price paper quotes share the queue ahead.

The queue is a deliberately simple approximation: later public cancellations do
not advance it, hidden liquidity is absent, and our simulated orders never alter
the actual public book. A through print's volume is still a single shared budget.
These choices cannot establish whether a real order would have filled. Fees,
funding, latency, margin and liquidation remain excluded.

Source timestamps must be strictly later than the local quote placement time,
its initial book cut and the last processed trade cut. This discards historical
subscription prints and conservatively skips same-millisecond ambiguous fills.
Duplicate trade IDs at a cut and older source cuts cannot replay liquidity.
Up to 4096 distinct prints per symbol/millisecond are accepted; overflow stops the
session. Malformed prints, subscription refusal, stale books or a disconnected
socket stop the run and cancel simulated quotes through the usual cleanup.
Book updates mark positions but do not also fill resting tape-driven quotes.

The `record`/`replay` commands currently record books only. They retain their
existing book-depth crossing model, not this tape model. They cannot reconstruct
missing public prints or true queue position. The synthetic public-wire tape in
`tests/fixtures/paper-trades.jsonl` deterministically exercises queue consumption,
partial fills, price/time allocation, closing fills and simulated PnL. It is test
data, not captured historical market data or a profitability claim.
