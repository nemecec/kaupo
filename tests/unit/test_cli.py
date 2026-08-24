"""CLI tests: typer runner with exchange/DB calls monkeypatched out."""

from datetime import UTC, datetime
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


EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"


def test_strategies_command() -> None:
    result = runner.invoke(cli.app, ["strategies", "--strategies-dir", str(EXAMPLES_DIR)])
    assert result.exit_code == 0
    assert "regime-switch" in result.output


def test_lint_strategies_clean() -> None:
    result = runner.invoke(cli.app, ["lint-strategies", "--strategies-dir", str(EXAMPLES_DIR)])
    assert result.exit_code == 0
    assert "No violations" in result.output


def test_lint_strategies_violation(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("import requests\n")
    result = runner.invoke(cli.app, ["lint-strategies", "--strategies-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "requests" in result.output


def _patch_coverage(
    monkeypatch: pytest.MonkeyPatch,
    first: datetime | None,
    last: datetime | None,
    count: int,
    seen: dict[str, Any] | None = None,
) -> None:
    """Cut the DB out of the ingest coverage report."""
    import kaupo.data.candles as candles_mod
    import kaupo.db.session as session_mod

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: FakeSession)

    async def fake_range(session: Any, pair: Any, tf: Any, exchange: str = "kraken") -> tuple[Any, Any, int]:
        if seen is not None:
            seen["exchange"] = exchange
        return first, last, count

    monkeypatch.setattr(candles_mod, "get_candle_range", fake_range)


class _FakeKrakenClient:
    async def __aenter__(self) -> "_FakeKrakenClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


class _FakeBinanceClient:
    async def __aenter__(self) -> "_FakeBinanceClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def test_ingest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.ingest as ingest_mod
    import kaupo.data.kraken as kraken_mod

    calls: dict[str, Any] = {}

    async def fake_backfill(client: Any, sm: Any, pair: Any, tf: Any, start: Any, end: Any) -> int:
        calls.update(pair=str(pair), tf=tf.value)
        return 42

    monkeypatch.setattr(kraken_mod, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(ingest_mod, "backfill", fake_backfill)
    _patch_coverage(monkeypatch, datetime(2025, 8, 24, tzinfo=UTC), datetime(2026, 8, 24, tzinfo=UTC), 8760)

    result = runner.invoke(cli.app, ["ingest", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "42 candles" in result.output
    assert "Database coverage: 8760 candles" in result.output
    assert "720 newest" not in result.output
    assert calls == {"pair": "BTC/EUR", "tf": "1h"}


def test_ingest_warns_on_partial_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.ingest as ingest_mod
    import kaupo.data.kraken as kraken_mod

    async def fake_backfill(client: Any, sm: Any, pair: Any, tf: Any, start: Any, end: Any) -> int:
        return 720

    monkeypatch.setattr(kraken_mod, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(ingest_mod, "backfill", fake_backfill)
    _patch_coverage(monkeypatch, datetime(2026, 7, 25, tzinfo=UTC), datetime(2026, 8, 24, tzinfo=UTC), 720)

    result = runner.invoke(cli.app, ["ingest", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "365"])
    assert result.exit_code == 0, result.output
    assert "Database coverage: 720 candles" in result.output
    assert "720 newest candles" in result.output


def test_ingest_binance_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.binance as binance_mod
    import kaupo.data.ingest as ingest_mod

    used: dict[str, Any] = {}

    async def fake_backfill(client: Any, sm: Any, pair: Any, tf: Any, start: Any, end: Any) -> int:
        used["is_binance"] = isinstance(client, _FakeBinanceClient)
        return 57_000

    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
    monkeypatch.setattr(ingest_mod, "backfill", fake_backfill)
    seen: dict[str, Any] = {}
    _patch_coverage(
        monkeypatch, datetime(2020, 1, 3, tzinfo=UTC), datetime(2026, 8, 24, tzinfo=UTC), 57_000, seen
    )

    result = runner.invoke(
        cli.app,
        ["ingest", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "2400", "--exchange", "binance"],
    )
    assert result.exit_code == 0, result.output
    assert used["is_binance"] is True
    assert seen["exchange"] == "binance"
    assert "from binance" in result.output
    assert "720 newest" not in result.output


def test_ingest_rejects_unknown_exchange() -> None:
    result = runner.invoke(cli.app, ["ingest", "--pair", "BTC/EUR", "--exchange", "coinbase"])
    assert result.exit_code == 2
    assert "binance, kraken" in result.output


def test_backtest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.run as bt_mod

    captured: dict[str, Any] = {}

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        captured["exchange"] = request.exchange
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
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run-1" in result.output
    assert "num_fills" in result.output
    assert captured["exchange"] == "kraken"  # default


def test_backtest_exchange_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.run as bt_mod

    captured: dict[str, Any] = {}

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        captured["exchange"] = request.exchange
        return RunId("run-1"), FakeResult(), {"num_fills": 0}

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
            "--exchange",
            "binance",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["exchange"] == "binance"


def test_backtest_unknown_strategy() -> None:
    result = runner.invoke(
        cli.app,
        ["backtest", "--strategy", "nope", "--pair", "BTC/EUR", "--strategies-dir", str(EXAMPLES_DIR)],
    )
    assert result.exit_code == 1
    assert "Unknown strategy" in result.output


def test_run_shadow_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.core.runner as runner_mod
    import kaupo.data.kraken as kraken_mod
    import kaupo.data.settings as settings_mod

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

    async def fake_resolve(session: Any, strategy: Any, pair: Any, timeframe: Any) -> Any:
        return settings_mod.ShadowSettings(
            strategy=strategy or "regime-switch",
            pair=pair or "BTC/EUR",
            timeframe=timeframe or "1h",
        )

    async def fake_run_shadow(request: Any, sm: Any, client: Any, stop: Any = None) -> Any:
        assert request.pair == "BTC/EUR" or str(request.pair) == "BTC/EUR"
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(runner_mod, "run_shadow", fake_run_shadow)
    monkeypatch.setattr(settings_mod, "resolve_shadow_config", fake_resolve)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Shadow run ended" in result.output


def test_run_shadow_no_flags_uses_resolved_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flags are optional: with none given, the resolved DB/default config is used."""
    import kaupo.core.runner as runner_mod
    import kaupo.data.kraken as kraken_mod
    import kaupo.data.settings as settings_mod

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    class FakeResult:
        status = type("S", (), {"value": "halted"})()
        num_fills = 0
        final_equity = 10_000
        halt_reason = "strategy switch requested"

    captured: dict[str, Any] = {}

    async def fake_resolve(session: Any, strategy: Any, pair: Any, timeframe: Any) -> Any:
        captured["resolve_args"] = (strategy, pair, timeframe)
        return settings_mod.ShadowSettings(strategy="regime-switch", pair="BTC/EUR", timeframe="1h")

    async def fake_run_shadow(request: Any, sm: Any, client: Any, stop: Any = None) -> Any:
        captured["strategy_id"] = request.strategy.id
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(runner_mod, "run_shadow", fake_run_shadow)
    monkeypatch.setattr(settings_mod, "resolve_shadow_config", fake_resolve)

    result = runner.invoke(cli.app, ["run", "shadow", "--strategies-dir", str(EXAMPLES_DIR)])
    assert result.exit_code == 0, result.output
    assert captured["resolve_args"] == (None, None, None)
    assert captured["strategy_id"] == "regime-switch"


def test_run_shadow_unknown_resolved_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored strategy id that is not in the strategies dir fails fast."""
    import kaupo.data.settings as settings_mod

    async def fake_resolve(session: Any, strategy: Any, pair: Any, timeframe: Any) -> Any:
        return settings_mod.ShadowSettings(strategy="nope", pair="BTC/EUR", timeframe="1h")

    monkeypatch.setattr(settings_mod, "resolve_shadow_config", fake_resolve)

    result = runner.invoke(cli.app, ["run", "shadow", "--strategies-dir", str(EXAMPLES_DIR)])
    assert result.exit_code == 1
    assert "Unknown strategy" in result.output


def test_backtest_lint_enforced_cli(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("from time import time\nx = time()\n")
    result = runner.invoke(
        cli.app,
        ["backtest", "--strategy", "bad", "--pair", "BTC/EUR", "--strategies-dir", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "wall-clock" in result.output


def test_strategies_missing_dir() -> None:
    result = runner.invoke(cli.app, ["lint-strategies", "--strategies-dir", "/nope/nada"])
    assert result.exit_code != 0
