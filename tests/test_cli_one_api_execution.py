"""CLI -> runner -> strategy -> risk -> actual transport/simulator, entirely offline."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
import yaml
from eth_account import Account as EthAccount
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from live_fake import SequencedFake
from market_receipt import approved_market

from glyde_mm import cli
from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, load_config
from glyde_mm.paper import PaperExchange
from glyde_mm.runner import run

D = Decimal
OWNER = "0x" + "11" * 20
VAULT = "0x" + "22" * 20


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("CLI execution tests must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)


def exercise_cli(
    monkeypatch,
    tmp_path,
    *,
    command,
    risk=None,
    existing_position=False,
    existing_order=False,
    understand=False,
    paper_fill_sides=("buy",),
):
    sequenced = SequencedFake(read_credential="synthetic-read-credential")
    fake = sequenced.ledger
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    if existing_position:
        fake.market_order("BTC", buy=True, size="0.1", user=OWNER)
    if existing_order:
        fake.exchange(
            {
                "action": {
                    "type": "order",
                    "orders": [
                        {
                            "a": 0,
                            "b": True,
                            "p": "98",
                            "s": "0.1",
                            "r": False,
                            "t": {"limit": {"tif": "Gtc"}},
                            "c": "0x" + "d" * 32,
                        }
                    ],
                }
            },
            user=OWNER,
        )
    wallet = EthAccount.create()
    token = "synthetic-read-credential"
    if command == "run":
        monkeypatch.setattr("builtins.input", lambda _: "BTC")
        monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", wallet.key.hex())
        monkeypatch.setenv("GLYDE_READ_API_KEY", token)
    else:
        monkeypatch.delenv("GLYDE_API_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("GLYDE_READ_API_KEY", raising=False)
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                # The paper command must force simulation even for an explicit live YAML.
                "mode": "live",
                "owner": OWNER,
                "vault_address": VAULT,
                "symbol": "BTC",
                "base_url": "https://offline.invalid/v1",
                "strategy": "pure_market_making",
                "strategy_config": {
                    "bid_spread": "0.002",
                    "ask_spread": "0.002",
                    "order_amount": "0.1",
                },
                "risk": {"max_position": "1", "max_order_notional": "25", **(risk or {})},
            }
        )
    )
    actions, reports, papers, paper_quotes = [], [], [], []
    secret = Config.secret

    def observed_secret(config, name):
        assert config.mode != "paper", "paper must not read private credentials"
        return secret(config, name)

    monkeypatch.setattr(Config, "secret", observed_secret)

    async def controlled_run(
        config,
        *,
        state_directory,
        stop_event,
        allow_existing_exposure=False,
    ):
        class ObservedPaper(PaperExchange):
            async def place_orders(self, orders):
                result = await super().place_orders(orders)
                resting = await self.open_orders()
                assert len(resting) == 2
                paper_quotes.extend(resting)
                for side in paper_fill_sides:
                    fake.advance_time(max(1, time.time_ns() // 1_000_000 - fake.now_ms + 1))
                    fake.market_order("BTC", buy=side == "sell", size="0.1")
                return result

            async def on_trade(self, trade):
                await super().on_trade(trade)
                if len(self.fills) == len(paper_fill_sides):
                    stop_event.set()

        def paper_factory(balance):
            paper = ObservedPaper(balance)
            papers.append(paper)
            paper.observed_quotes = paper_quotes
            return paper

        @asynccontextmanager
        async def client_factory(selected):
            transport = fake.http_transport()

            async def handle(request):
                body = json.loads(request.content)
                if request.url.path.endswith("/exchange"):
                    assert config.mode == "live"
                    assert body["vaultAddress"] == VAULT
                    signer = recover_agent_or_user_from_l1_action(
                        body["action"], body["signature"], VAULT, body["nonce"], None, True
                    )
                    assert signer == wallet.address
                    actions.append(body)
                else:
                    private = body["type"] in {"clearinghouseState", "openOrders"}
                    assert request.headers.get("X-Glyde-API-Key") == (token if private else None)
                response = await transport.handle_async_request(request)
                if request.url.path.endswith("/exchange"):
                    sequenced.emit_owner()
                if request.url.path.endswith("/exchange") and body["action"]["type"] == "order":
                    assert len(fake.info({"type": "openOrders", "user": OWNER})) == 2 + int(
                        existing_order
                    )
                    stop_event.set()
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                async with OneApiClient(
                    selected,
                    http_client=http,
                    connect_ws=sequenced.connect if selected.mode == "live" else fake.connect_ws,
                ) as client:
                    yield client

        async with asyncio.timeout(3):
            await run(
                config,
                state_directory=state_directory,
                client_factory=client_factory,
                paper_factory=paper_factory,
                stop_event=stop_event,
                report=reports.append,
                allow_existing_exposure=allow_existing_exposure,
            )

    if command == "run":
        approved_market(load_config(path), tmp_path / ".glyde-mm")
    monkeypatch.setattr(cli, "run_strategy", controlled_run)
    result = cli.main([command, str(path)] + (["--i-understand"] if understand else []))
    assert not fake._connections
    if actions:
        assert sequenced.sequence == 1 + len(actions)
    return result, fake, actions, reports, papers


def test_paper_cli_quotes_fill_real_simulator_and_cancel_on_exit(monkeypatch, tmp_path):
    result, fake, actions, reports, papers = exercise_cli(monkeypatch, tmp_path, command="paper")
    assert result == 0 and actions == []
    assert all(row["body"]["type"] == "meta" for row in fake.requests)
    paper = papers[0]
    assert len(paper.fills) == 1
    assert paper.fills[0].side == "buy"
    assert paper.fills[0].quantity == D("0.1") and paper.fills[0].price == D("99.8")
    account, remaining = asyncio.run(paper.account()), asyncio.run(paper.open_orders())
    assert account.positions == {"BTC": D("0.1")} and remaining == []
    assert account.equity == D("10000.02")
    assert any("PAPER SUMMARY" in line and "fills=1 | open_quotes=0" in line for line in reports)
    assert fake.info({"type": "openOrders", "user": OWNER}) == []


def test_paper_cli_both_quotes_fill_through_public_tape_without_keys(monkeypatch, tmp_path):
    result, fake, actions, reports, papers = exercise_cli(
        monkeypatch, tmp_path, command="paper", paper_fill_sides=("buy", "sell")
    )
    paper = papers[0]
    assert result == 0 and actions == []
    assert [(row.side, row.quantity, row.price) for row in paper.observed_quotes] == [
        ("buy", D("0.1"), D("99.8")),
        ("sell", D("0.1"), D("100.2")),
    ]
    assert [(row.side, row.quantity, row.price) for row in paper.fills] == [
        ("buy", D("0.1"), D("99.8")),
        ("sell", D("0.1"), D("100.2")),
    ]
    account = asyncio.run(paper.account())
    assert account.positions.get("BTC", D(0)) == 0
    assert account.equity == D("10000.04")
    assert asyncio.run(paper.open_orders()) == []
    assert any("PAPER SUMMARY" in row and "fills=2 | open_quotes=0" in row for row in reports)
    assert all(row["body"]["type"] == "meta" for row in fake.requests)
    assert fake.info({"type": "openOrders", "user": OWNER}) == []


@pytest.mark.parametrize("risk", [{"max_position": "0.05"}, {"max_order_notional": "1"}])
def test_paper_cli_risk_refusal_never_places_simulated_or_real_orders(monkeypatch, tmp_path, risk):
    result, fake, actions, reports, papers = exercise_cli(
        monkeypatch, tmp_path, command="paper", risk=risk
    )
    paper = papers[0]
    assert result == 1 and actions == []
    assert paper.observed_quotes == [] and paper.fills == []
    assert asyncio.run(paper.open_orders()) == []
    assert (asyncio.run(paper.account())).equity == D("10000")
    assert any("PAPER SUMMARY" in row and "fills=0 | open_quotes=0" in row for row in reports)
    assert all(row["body"]["type"] == "meta" for row in fake.requests)


def test_live_cli_signs_places_and_cancels_through_actual_one_api(monkeypatch, tmp_path):
    result, fake, actions, _, papers = exercise_cli(monkeypatch, tmp_path, command="run")
    assert result == 0 and papers == []
    assert [row["action"]["type"] for row in actions] == ["order", "cancel", "cancel"]
    orders = actions[0]["action"]["orders"]
    assert [(row["b"], row["p"], row["s"]) for row in orders] == [
        (True, "99.8", "0.1"),
        (False, "100.2", "0.1"),
    ]
    assert len({row["c"] for row in orders}) == 2
    nonces = [row["nonce"] for row in actions]
    assert nonces == sorted(set(nonces))
    cancellations = [row["action"]["cancels"][0]["o"] for row in actions[1:]]
    assert len(set(cancellations)) == 2
    assert fake.info({"type": "openOrders", "user": OWNER}) == []
    assert fake.info({"type": "userFills", "user": OWNER}) == []


@pytest.mark.parametrize("risk", [{"max_position": "0.05"}, {"max_order_notional": "1"}])
def test_live_cli_risk_rejection_never_reaches_exchange(monkeypatch, tmp_path, risk):
    result, fake, actions, _, papers = exercise_cli(monkeypatch, tmp_path, command="run", risk=risk)
    assert result == 1 and actions == [] and papers == []
    assert all(row["path"].endswith("/info") for row in fake.requests)
    assert fake.info({"type": "openOrders", "user": OWNER}) == []


@pytest.mark.parametrize("position,order", [(True, False), (False, True), (True, True)])
def test_live_preflight_refuses_other_exposure_without_cancelling_it(
    monkeypatch,
    tmp_path,
    position,
    order,
    capsys,
):
    result, fake, actions, _, papers = exercise_cli(
        monkeypatch,
        tmp_path,
        command="run",
        existing_position=position,
        existing_order=order,
    )
    assert result == 1 and actions == [] and papers == []
    remaining = fake.info({"type": "openOrders", "user": OWNER})
    assert len(remaining) == int(order)
    assert "--i-understand" in capsys.readouterr().err
    if position:
        state = fake.info({"type": "clearinghouseState", "user": OWNER})
        assert state["assetPositions"][0]["position"]["szi"] == "0.1"


def test_live_preflight_override_allows_start_but_retains_actual_risk(monkeypatch, tmp_path):
    result, _, actions, _, papers = exercise_cli(
        monkeypatch,
        tmp_path,
        command="run",
        existing_position=True,
        understand=True,
    )
    assert result == 0 and papers == []
    assert actions[0]["action"]["type"] == "order"


def test_live_preflight_override_cannot_bypass_position_cap(monkeypatch, tmp_path):
    result, _, actions, _, _ = exercise_cli(
        monkeypatch,
        tmp_path,
        command="run",
        existing_position=True,
        understand=True,
        risk={"max_position": "0.05"},
    )
    assert result == 1 and actions == []
