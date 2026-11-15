"""Causal smoke-report controls and the actual loopback HTTP/WS fixture."""

import importlib.util
import json
from pathlib import Path

import aiohttp
import pytest

from glyde_mm.testing.local_server import LocalOneApi

spec = importlib.util.spec_from_file_location(
    "container_smoke", Path(__file__).parents[1] / "scripts/container-smoke.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

GOOD = [
    "PAPER BTC | inventory=0 | simulated_pnl=0 | fills=0 | open_quotes=2 | risk_stop=none",
    "PAPER SUMMARY BTC | inventory=0 | simulated_pnl=0.04 | fills=2 | "
    "open_quotes=0 | risk_stop=none",
]


def test_quote_and_cleanup_working_control():
    smoke.check_transcript(GOOD, 0)
    smoke.check_image({"User": "maker", "Env": ["PATH=/usr/bin"]})


@pytest.mark.parametrize(
    "lines,code",
    [
        (GOOD[1:], 0),
        (GOOD[:1], 0),
        ([GOOD[0], GOOD[1].replace("quotes=0", "quotes=1")], 0),
        ([GOOD[0], GOOD[1].replace("fills=2", "fills=0")], 0),
        (GOOD, 1),
    ],
)
def test_unplaced_unclean_or_failed_container_never_passes(lines, code):
    with pytest.raises(AssertionError):
        smoke.check_transcript(lines, code)


@pytest.mark.parametrize("user", ["", "root", "0", "0:0", "0:10001", None])
def test_root_container_never_passes(user):
    with pytest.raises(AssertionError):
        smoke.check_image({"User": user})


@pytest.mark.parametrize("name", sorted(smoke.PRIVATE_ENV))
def test_baked_credentials_never_pass(name):
    with pytest.raises(AssertionError):
        smoke.check_image({"User": "maker", "Env": [name + "=synthetic"]})


async def test_fake_public_http_ws_stream_and_forbidden_financial_route():
    async with LocalOneApi() as fixture:
        async with aiohttp.ClientSession() as client:
            async with client.post(fixture.base_url + "/info", json={"type": "meta"}) as response:
                assert response.status == 200
                assert (await response.json())["universe"][0]["name"] == "BTC"
            async with client.ws_connect(fixture.base_url + "/ws") as socket:
                for kind in ["l2Book", "trades"]:
                    await socket.send_json(
                        {
                            "method": "subscribe",
                            "subscription": {"type": kind, "coin": "BTC"},
                        }
                    )
                channels = set()
                for _ in range(12):
                    frame = json.loads((await socket.receive(timeout=2)).data)
                    if frame["channel"] == "l2Book" or (
                        frame["channel"] == "trades" and frame["data"]
                    ):
                        channels.add(frame["channel"])
                    if channels == {"l2Book", "trades"}:
                        break
                assert channels == {"l2Book", "trades"}
            async with client.post(fixture.base_url + "/exchange", json={}) as response:
                assert response.status == 403
            async with client.post(
                fixture.base_url + "/info", json={"type": "clearinghouseState"}
            ) as response:
                assert response.status == 403
        assert fixture.forbidden == ["/v1/exchange", "/v1/info"]
    assert not fixture.fake._connections


def test_publish_requires_smoke_and_only_github_token_with_both_architectures():
    import yaml

    root = Path(__file__).parents[1]
    release = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    publish = release["jobs"]["publish-container"]
    assert set(publish["needs"]) == {"release", "container-smoke"}
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    login = next(row for row in publish["steps"] if "docker/login-action@" in row.get("uses", ""))
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    build = next(
        row for row in publish["steps"] if "docker/build-push-action@" in row.get("uses", "")
    )
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert "${{ github.ref_name }}" in build["with"]["tags"]
    assert "sha-${{ github.sha }}" in build["with"]["tags"]
