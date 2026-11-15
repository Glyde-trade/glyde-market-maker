# Frozen public One API compatibility transcript

`session.jsonl` records a **synthetic, socket-free** `FakeOneApiServer` session from
current source. It is not a production capture, historical market data or live
compatibility evidence. There are no credentials, private channels, account IDs,
headers, balances, signatures or internal URLs in this fixture.

The session initializes BTC/ETH metadata and fixed decimal depth at timestamp
1700000000000, records HTTP `meta`, `l2Book` and `allMids`, then subscribes to
`l2Book`, `allMids` and `trades`. It retains the real fake-server subscription ACKs
and initial snapshots, including an empty trade list. One simulated buy at the next
millisecond creates a trade, book update and mids update. Its deliberately advanced
trade counter proves an integer ID beyond 2^53 survives parsing exactly.

The replay tests inject these frozen bytes into the **real OneApiClient**, without
importing or running the fake server. Unknown HTTP requests/private reads fail;
socket calls and credential access are forbidden. Exact parsed values, subscription
requests, ACKs, frame order, HTTP/WS agreement and malformed/refused controls make
client wire drift visible offline. Keeping the recording fixed prevents a fake-server
and client change from silently updating each other's expectations.

A production-public capture can be added after the service is announced live. Keep
only these allowlisted public methods/channels, document provenance, and review the
wire change rather than regenerating expected output to silence a failing test.
This suite cannot establish that a deployment is reachable or matches this transcript.

Run: `pytest -q tests/test_public_api_compatibility.py`.
