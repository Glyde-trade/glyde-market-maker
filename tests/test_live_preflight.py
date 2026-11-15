from __future__ import annotations

import pytest

from glyde_mm import cli


@pytest.mark.parametrize("understand", [False, True])
def test_live_preflight_needs_exact_symbol_even_with_override(
    tmp_path,
    monkeypatch,
    capsys,
    understand,
):
    path = tmp_path / "live.yaml"
    path.write_text("mode: live\nsymbol: BTC\n")
    monkeypatch.setattr("builtins.input", lambda _: "btc")

    async def forbidden(*args, **kwargs):
        raise AssertionError("confirmation refusal must occur before the runner")

    monkeypatch.setattr(cli, "run_with_signals", forbidden)
    args = ["run", str(path)] + (["--i-understand"] if understand else [])
    assert cli.main(args) == 1
    assert "confirmation" in capsys.readouterr().err.lower()


def test_live_preflight_eof_refuses_unattended_start(tmp_path, monkeypatch, capsys):
    path = tmp_path / "live.yaml"
    path.write_text("mode: live\n")

    def eof(_):
        raise EOFError

    async def forbidden(*args, **kwargs):
        raise AssertionError("EOF must occur before the runner")

    monkeypatch.setattr("builtins.input", eof)
    monkeypatch.setattr(cli, "run_with_signals", forbidden)
    assert cli.main(["run", str(path)]) == 1
    assert "confirmation" in capsys.readouterr().err.lower()


def test_paper_preflight_prints_limits_without_prompt_or_key_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "paper.yaml"
    path.write_text(
        'mode: paper\nsymbol: BTC\nrisk: {max_position: "0.2", '
        'max_order_notional: "17", max_daily_loss: "3"}\n'
    )
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "never-print-this-signing-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "never-print-this-read-key")

    def forbidden(_):
        raise AssertionError("paper must not ask for live confirmation")

    async def no_op(*args, **kwargs):
        pass

    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(cli, "run_with_signals", no_op)
    assert cli.main(["paper", str(path)]) == 0
    output = capsys.readouterr().out
    assert "mode=PAPER" in output and "symbol=BTC" in output
    assert "max_position=0.2" in output
    assert "max_order_notional=17" in output
    assert "max_daily_loss=3" in output
    assert "never-print-this" not in output


async def test_preflight_scope_is_only_the_selected_symbol():
    from decimal import Decimal as D

    from glyde_mm.models import Account, OpenOrder
    from glyde_mm.preflight import check_live_exposure

    class Source:
        async def account(self):
            return Account(D(1000), {"ETH": D(3), "BTC": D(0)})

        async def open_orders(self):
            return [OpenOrder("other", "ETH", "buy", D(1), D(100))]

    await check_live_exposure(Source(), "BTC")


@pytest.mark.parametrize("position", ["0", float("nan")])
async def test_override_does_not_accept_malformed_position(position):
    from decimal import Decimal as D

    from glyde_mm.models import Account
    from glyde_mm.preflight import check_live_exposure
    from glyde_mm.risk import RiskViolation

    class Source:
        async def account(self):
            return Account(D(1000), {"BTC": position})

        async def open_orders(self):
            return []

    with pytest.raises(RiskViolation, match="invalid_account"):
        await check_live_exposure(Source(), "BTC", allow_existing_exposure=True)


async def test_preflight_read_failure_cannot_enter_cancellation_cleanup(tmp_path, monkeypatch):
    from glyde_mm.client import OneApiError
    from glyde_mm.config import Config
    from glyde_mm.runner import run

    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read")
    cancellations = []

    class Source:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def account(self):
            raise OneApiError("private read unavailable")

        async def cancel_all(self, *args):
            cancellations.append(args)

    selected = Config(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)
    with pytest.raises(OneApiError, match="private read unavailable"):
        await run(
            selected,
            state_directory=tmp_path,
            client_factory=lambda _: Source(),
            report=lambda _: None,
        )
    assert cancellations == []


async def test_stop_during_live_preflight_does_not_cancel_existing_orders(tmp_path, monkeypatch):
    import asyncio
    from decimal import Decimal as D

    from glyde_mm.config import Config
    from glyde_mm.models import Account
    from glyde_mm.runner import run

    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read")
    stop = asyncio.Event()
    cancellations = []
    reads = []

    class Source:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def account(self):
            return Account(D(1000), {})

        async def open_orders(self):
            reads.append("orders")
            stop.set()
            return []

        async def cancel_all(self, *args):
            cancellations.append(args)

    selected = Config(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)
    await run(
        selected,
        state_directory=tmp_path,
        client_factory=lambda _: Source(),
        stop_event=stop,
        report=lambda _: None,
    )
    assert cancellations == []
    assert reads == ["orders"]
