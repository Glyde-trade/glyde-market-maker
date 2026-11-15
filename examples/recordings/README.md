# Offline paper-session fixture

`paper-session.jsonl` is **synthetic public-book test data**, not historical BTC
prices or a market-profit claim. It was recorded through `record_books` using the
in-process `FakeOneApiServer`, BTC size precision 2 and a virtual receipt clock.
No real server, private account, credentials or network was used.

The 61 one-second observations start at fixture timestamp 1700000000000.
Bid/ask is 99/101, changes to 96/98 at second 1, to 102/104 at second 2,
then returns to 99/101 through second 60. Each side has two base units of depth.
The source and receipt timestamps are retained. These deliberate moves give the
shared paper strategy two deterministic fills so a successful report is visible.

Use only with `examples/paper-session.yaml` and the offline `replay` command.
For your own market experiments, use `record` to capture actual public books first;
a current book cannot reconstruct historical depth.
