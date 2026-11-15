"""Run the installed CLI in a real non-root container against a loopback fake One API."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import yaml

from glyde_mm.testing.local_server import LocalOneApi

PRIVATE_ENV = {"GLYDE_API_PRIVATE_KEY", "GLYDE_READ_API_KEY", "GLYDE_MM_ALERT_WEBHOOK"}


def check_image(config):
    if str(config.get("User", "")).split(":", 1)[0] in {"None", "", "root", "0"}:
        raise AssertionError("image must default to a non-root user")
    if any(row.split("=", 1)[0] in PRIVATE_ENV for row in config.get("Env", [])):
        raise AssertionError("image contains a credential environment variable")


def check_transcript(lines, returncode):
    if returncode != 0:
        raise AssertionError(f"paper container exited {returncode}")
    if not any(re.search(r"^PAPER BTC .*open_quotes=[1-9]\d*", row) for row in lines):
        raise AssertionError("no simulated quotes were observed")
    summaries = [row for row in lines if row.startswith("PAPER SUMMARY BTC ")]
    if not summaries or not re.search(r"open_quotes=0(?:\s|$)", summaries[-1]):
        raise AssertionError("paper cleanup was not confirmed")
    if not any(re.search(r"fills=[1-9]\d*", row) for row in summaries):
        raise AssertionError("fake public tape produced no paper fills")


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(60):
            out, err = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')}")
    return out.decode()


async def smoke(image, seconds=20):
    if seconds < 20:
        raise ValueError("container acceptance requires at least 20 seconds of quoting")
    inspected = json.loads(await command("docker", "image", "inspect", image))[0]["Config"]
    check_image(inspected)
    # The image has no dev dependencies or fixture injected into its installed CLI.
    identity = await command(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        image,
        "-c",
        "import os,pathlib; assert os.getuid()!=0; "
        "roots=[pathlib.Path(p) for p in ['/app','/work','/home/maker']]; "
        "assert not any(p.is_file() and (p.name.startswith('.env') or "
        "p.name in {'id_rsa','.netrc','credentials'}) for r in roots for p in r.rglob('*')); "
        "print('PASS: non-root UID',os.getuid(),'and no private fixture files')",
    )
    print(identity.strip(), flush=True)
    async with LocalOneApi() as fixture:
        with tempfile.TemporaryDirectory(prefix="glyde-mm-container-") as directory:
            config = Path(directory) / "paper.yaml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "mode": "paper",
                        "base_url": fixture.base_url,
                        "symbol": "BTC",
                        "strategy": "pure_market_making",
                        "strategy_config": {
                            "order_amount": "0.1",
                            "bid_spread": "0.002",
                            "ask_spread": "0.002",
                            "order_refresh_time": 1,
                        },
                        "risk": {
                            "max_position": "1",
                            "max_order_notional": "25",
                            "max_orders_per_minute": 600,
                        },
                    }
                )
            )
            config.chmod(0o644)
            name = "glyde-mm-smoke-" + uuid4().hex
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                "host",
                "--init",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--tmpfs",
                "/work/.glyde-mm:uid=10001,gid=10001,mode=0700",
                "--tmpfs",
                "/tmp:mode=1777",
                "--env",
                "PYTHONUNBUFFERED=1",
                "--mount",
                f"type=bind,source={config},target=/work/paper.yaml,readonly",
                image,
                "paper",
                "/work/paper.yaml",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            lines = []
            quoting = asyncio.Event()

            async def read():
                async for raw in process.stdout:
                    row = raw.decode(errors="replace").strip()
                    lines.append(row)
                    print(row, flush=True)
                    if re.search(r"^PAPER BTC .*open_quotes=[1-9]\d*", row):
                        quoting.set()
                if not quoting.is_set():
                    raise AssertionError("container exited before quoting")

            reader = asyncio.create_task(read())
            try:
                async with asyncio.timeout(60 + seconds):
                    waiter = asyncio.create_task(quoting.wait())
                    try:
                        done, _ = await asyncio.wait(
                            [waiter, reader], timeout=30, return_when=asyncio.FIRST_COMPLETED
                        )
                        if reader in done:
                            await reader
                        if not quoting.is_set():
                            raise AssertionError("container did not quote within 30 seconds")
                    finally:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                    await asyncio.sleep(seconds)
                    if process.returncode is not None:
                        raise AssertionError("container exited before the 20-second stop")
                    await command("docker", "kill", "--signal=SIGTERM", name)
                    await asyncio.wait_for(process.wait(), 30)
                    await reader
                    check_transcript(lines, process.returncode)
                    if fixture.forbidden:
                        raise AssertionError("paper requested a private or exchange route")
                    print(f"PASS: {seconds}s paper quotes/fills/cleanup; public loopback only")
            finally:
                if process.returncode is None:
                    await command("docker", "rm", "--force", name)
                    await process.wait()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args.image))
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from None
