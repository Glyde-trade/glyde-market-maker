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

    def workflow(name):
        return yaml.load((root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    release = workflow("release.yml")
    assert release["on"]["push"]["tags"] == ["v*"]
    caller = release["jobs"]["publish-container"]
    assert caller["needs"] == "release"  # Matching tag/version and checked wheel remain required.
    assert caller["uses"] == "./.github/workflows/publish-container.yml"
    assert caller["permissions"] == {"contents": "read", "packages": "write"}
    container = workflow("publish-container.yml")
    assert set(container["on"]) == {"push", "workflow_call"}
    assert container["on"]["push"] == {"branches": ["master"]}
    assert container["permissions"] == {"contents": "read"}
    assert container["jobs"]["container-smoke"]["uses"] == (
        "./.github/workflows/container-smoke.yml"
    )
    publish = container["jobs"]["publish"]
    assert publish["needs"] == "container-smoke"
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    login = next(row for row in publish["steps"] if "docker/login-action@" in row.get("uses", ""))
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    build = next(
        row for row in publish["steps"] if "docker/build-push-action@" in row.get("uses", "")
    )
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert "${{ github.ref_name }}" in build["with"]["tags"]
    assert "sha-${{ github.sha }}" in build["with"]["tags"]

    assert "secrets.GITHUB_TOKEN" not in build["with"].get("build-args", "")
    assert not {"secrets", "secret-envs", "secret-files"} & build["with"].keys()
    assert set(publish) <= {"needs", "runs-on", "timeout-minutes", "permissions", "steps"}
    assert [row["run"] for row in publish["steps"] if "run" in row] == [
        'python scripts/write-build-info.py "$GITHUB_SHA"'
    ]
    for row in publish["steps"]:
        if row.get("uses", "").startswith("actions/checkout@"):
            assert row["with"]["persist-credentials"] == "false"
        assert not row.get("env")


def test_every_pr_runs_offline_tests_types_and_ruff_on_hosted_runners():
    import yaml

    root = Path(__file__).parents[1]
    ci = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert "pull_request" in ci["on"] and ci["permissions"] == {"contents": "read"}
    test = ci["jobs"]["test"]
    assert test["strategy"]["fail-fast"] == "false"
    assert test["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13"]
    checks = {row["run"] for row in test["steps"] if "run" in row}
    assert {"ruff check .", "ruff format --check .", "mypy --strict src/glyde_mm"} <= checks
    assert 'pytest -q -m "not offline_soak"' in checks
    assert any(
        "tests/test_offline_soak.py" in row.get("run", "")
        for row in ci["jobs"]["offline-soak"]["steps"]
    )
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for job in workflow["jobs"].values():
            if "uses" in job:
                assert job["uses"].startswith("./.github/workflows/")
                assert (root / job["uses"]).is_file()
            else:
                assert job["runs-on"] == "ubuntu-latest"
