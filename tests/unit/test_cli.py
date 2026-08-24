"""CLI tests: typer runner with exchange/DB calls monkeypatched out."""

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import kaupo.cli.main as cli
from kaupo.domain import RunId

runner = CliRunner()


class TestHelpers:
    def test_parse_params(self) -> None:
        assert cli._parse_params(["a=1", "b=0.5", "c=true", "d=text"]) == {
            "a": 1,
            "b": 0.5,
            "c": True,
            "d": "text",
        }

    def test_parse_params_bad(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter):
            cli._parse_params(["no-equals-sign"])

    def test_range_defaults(self) -> None:
        start, end = cli._range(30, None, "2026-02-01T00:00:00+00:00")
        assert (end - start).days == 30
        assert end.tzinfo is UTC


def test_strategies_command() -> None:
    result = runner.invoke(cli.app, ["strategies"])
    assert result.exit_code == 0
    assert "regime-switch" in result.output


def test_lint_strategies_clean() -> None:
    result = runner.invoke(cli.app, ["lint-strategies"])
    assert result.exit_code == 0
    assert "No violations" in result.output


def test_lint_strategies_violation(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("import requests\n")
    result = runner.invoke(cli.app, ["lint-strategies", "--strategies-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "requests" in result.output


def test_ingest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.ingest as ingest_mod
    import kaupo.data.kraken as kraken_mod

    calls: dict[str, Any] = {}

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    async def fake_backfill(client: Any, sm: Any, pair: Any, tf: Any, start: Any, end: Any) -> int:
        calls.update(pair=str(pair), tf=tf.value)
        return 42

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(ingest_mod, "backfill", fake_backfill)

    result = runner.invoke(cli.app, ["ingest", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "42 candles" in result.output
    assert calls == {"pair": "BTC/EUR", "tf": "1h"}


def test_backtest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.run as bt_mod

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        metrics = {"num_fills": 3, "total_return_pct": 1.5}
        return RunId("run-1"), FakeResult(), metrics

    monkeypatch.setattr(bt_mod, "run_backtest", fake_run_backtest)

    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--days",
            "30",
            "--param",
            "adx_threshold=30",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run-1" in result.output
    assert "num_fills" in result.output


def test_backtest_unknown_strategy() -> None:
    result = runner.invoke(cli.app, ["backtest", "--strategy", "nope", "--pair", "BTC/EUR"])
    assert result.exit_code == 1
    assert "Unknown strategy" in result.output


def test_run_shadow_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.core.runner as runner_mod
    import kaupo.data.kraken as kraken_mod

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    class FakeResult:
        status = type("S", (), {"value": "halted"})()
        num_fills = 0
        final_equity = 10_000
        halt_reason = "stopped externally"

    async def fake_run_shadow(request: Any, sm: Any, client: Any, stop: Any = None) -> Any:
        assert request.pair == "BTC/EUR" or str(request.pair) == "BTC/EUR"
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(runner_mod, "run_shadow", fake_run_shadow)

    result = runner.invoke(cli.app, ["run", "shadow", "--strategy", "regime-switch", "--pair", "BTC/EUR"])
    assert result.exit_code == 0, result.output
    assert "Shadow run ended" in result.output
