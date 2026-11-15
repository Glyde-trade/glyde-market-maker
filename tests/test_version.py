from __future__ import annotations

import pytest

from glyde_mm import cli


def test_version_without_config_credentials_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("version must not read configuration or start trading")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "run_with_signals", forbidden)
    monkeypatch.setattr(cli, "build_identity", lambda: {"version": "2.3.4", "commit": "unknown"})
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == "glyde-mm 2.3.4\n"


def test_source_checkout_version_fallback(monkeypatch, capsys):
    from glyde_mm import version

    def not_installed(_):
        raise version.metadata.PackageNotFoundError("glyde-market-maker")

    monkeypatch.setattr(version.metadata, "distribution", not_installed)
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == f"glyde-mm {version.__version__}\n"
