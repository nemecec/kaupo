"""CLI tests: typer runner with exchange/DB calls monkeypatched out."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

# rich output wraps to the terminal width of the environment, which differs
# between local shells and CI. Pin it before the CLI module (and its console)
# is imported, so output assertions are deterministic.
os.environ["COLUMNS"] = "200"

import kaupo.cli.main as cli
from kaupo.domain import RunId

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling. CI renders with color; styled text splits substrings."""
    return _ANSI.sub("", text)


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

    result = runner.invoke(
        cli.app, ["ingest", "candles", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "7"]
    )
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

    result = runner.invoke(
        cli.app, ["ingest", "candles", "--pair", "BTC/EUR", "--timeframe", "1h", "--days", "365"]
    )
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
        [
            "ingest",
            "candles",
            "--pair",
            "BTC/EUR",
            "--timeframe",
            "1h",
            "--days",
            "2400",
            "--exchange",
            "binance",
        ],
    )
    assert result.exit_code == 0, result.output
    assert used["is_binance"] is True
    assert seen["exchange"] == "binance"
    assert "from binance" in result.output
    assert "720 newest" not in result.output


def test_ingest_rejects_unknown_exchange() -> None:
    result = runner.invoke(cli.app, ["ingest", "candles", "--pair", "BTC/EUR", "--exchange", "coinbase"])
    assert result.exit_code == 2
    assert "binance, kraken" in result.output


def _patch_funding_coverage(
    monkeypatch: pytest.MonkeyPatch,
    first: datetime | None,
    last: datetime | None,
    count: int,
    seen: dict[str, Any] | None = None,
) -> None:
    """Cut the DB out of the ingest funding coverage report."""
    import kaupo.data.funding as funding_mod
    import kaupo.db.session as session_mod

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: FakeSession)

    async def fake_range(session: Any, exchange: str, base_asset: str) -> tuple[Any, Any, int]:
        if seen is not None:
            seen["exchange"] = exchange
            seen["base_asset"] = base_asset
        return first, last, count

    monkeypatch.setattr(funding_mod, "get_funding_range", fake_range)


def test_ingest_funding_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.binance as binance_mod
    import kaupo.data.ingest as ingest_mod

    calls: dict[str, Any] = {}

    async def fake_backfill(client: Any, sm: Any, base_asset: str, start: Any, end: Any) -> int:
        calls.update(client_is_binance=isinstance(client, _FakeBinanceClient), base_asset=base_asset)
        return 1095

    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
    monkeypatch.setattr(ingest_mod, "backfill_funding", fake_backfill)
    seen: dict[str, Any] = {}
    _patch_funding_coverage(
        monkeypatch, datetime(2025, 8, 24, tzinfo=UTC), datetime(2026, 8, 24, tzinfo=UTC), 1095, seen
    )

    result = runner.invoke(cli.app, ["ingest", "funding", "--pair", "BTC/EUR", "--days", "365"])
    assert result.exit_code == 0, result.output
    assert "Ingested 1095 funding rates for BTC from binance" in result.output
    assert "Database coverage: 1095 funding rates" in result.output
    assert calls == {"client_is_binance": True, "base_asset": "BTC"}  # pair supplies the base asset
    assert seen == {"exchange": "binance", "base_asset": "BTC"}


def test_ingest_funding_rejects_kraken() -> None:
    result = runner.invoke(cli.app, ["ingest", "funding", "--pair", "BTC/EUR", "--exchange", "kraken"])
    assert result.exit_code == 2
    assert "only served by binance" in result.output


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


def test_backtest_stability_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.run as bt_mod
    import kaupo.backtest.stability as stab_mod

    captured: dict[str, Any] = {}

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        captured["full_stability"] = request.stability
        return RunId("run-full"), FakeResult(), {"num_fills": 3, "sharpe": 1.0}

    async def fake_slices(request: Any, sm: Any, *, group: str, windows: int) -> Any:
        captured["group"] = group
        captured["windows"] = windows
        return {
            "windows": windows,
            "slices": [
                {
                    "window": 0,
                    "start": "2026-01-01T00:00:00+00:00",
                    "end": "2026-01-16T00:00:00+00:00",
                    "run_id": "run-0",
                    "metrics": {
                        "sharpe": 1.2,
                        "max_drawdown_pct": -3.4,
                        "total_return_pct": 5.6,
                        "num_round_trips": 7,
                    },
                },
                {
                    "window": 1,
                    "start": "2026-01-16T00:00:00+00:00",
                    "end": "2026-01-31T00:00:00+00:00",
                    "error": "ValueError: No kraken candles for BTC/EUR 1h in range",
                },
            ],
        }

    monkeypatch.setattr(bt_mod, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(stab_mod, "run_stability_slices", fake_slices)
    # rich renders to the detected terminal size when the console counts as
    # a terminal (FORCE_COLOR / PY_COLORS in CI), ignoring width= and COLUMNS.
    # A non-terminal console with explicit width renders deterministically.
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=False))

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
            "--stability-windows",
            "2",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "run-full" in out
    # the full-window run carries the marker; the slices share its group
    assert captured["full_stability"] == {"group": captured["group"], "window": "full", "of": 2}
    assert captured["windows"] == 2
    # compact per-window table after the full-window metrics (console width
    # is pinned above, so full content renders)
    assert "Stability windows" in out
    assert "2026-01-01T00:00" in out
    assert "1.2" in out  # sharpe
    assert "-3.4" in out  # max DD
    assert "5.6" in out  # return
    assert "error: ValueError: No kraken candles" in out  # degraded slice


def test_backtest_stability_windows_out_of_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # typer's error panel console: force it non-terminal so its width setting
    # is honored (a "terminal" console renders to the detected size instead,
    # wrapping styled text mid-token in CI)
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", False)
    monkeypatch.setattr("typer.rich_utils.MAX_WIDTH", 200)
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--stability-windows",
            "1",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 2
    assert "--stability-windows" in _plain(result.output)


def test_backtest_no_stability_windows_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.run as bt_mod
    import kaupo.backtest.stability as stab_mod

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        assert request.stability is None  # no flag: today's behavior, no marker
        return RunId("run-1"), FakeResult(), {"num_fills": 0}

    async def fake_slices(request: Any, sm: Any, *, group: str, windows: int) -> Any:
        raise AssertionError("must not run slices")

    monkeypatch.setattr(bt_mod, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(stab_mod, "run_stability_slices", fake_slices)

    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Stability windows" not in result.output


def test_run_shadow_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.core.runner as runner_mod
    import kaupo.data.binance as binance_mod
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

    async def fake_run_shadow(
        request: Any, sm: Any, client: Any, stop: Any = None, funding_client: Any = None
    ) -> Any:
        assert request.pair == "BTC/EUR" or str(request.pair) == "BTC/EUR"
        assert isinstance(funding_client, _FakeBinanceClient)
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
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
    import kaupo.data.binance as binance_mod
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

    async def fake_run_shadow(
        request: Any, sm: Any, client: Any, stop: Any = None, funding_client: Any = None
    ) -> Any:
        captured["strategy_id"] = request.strategy.id
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", FakeClient)
    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
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


def test_run_shadow_static_flags_skip_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-config-from-db uses the flags as given and never resolves settings."""
    import kaupo.core.runner as runner_mod
    import kaupo.data.binance as binance_mod
    import kaupo.data.kraken as kraken_mod

    class FakeResult:
        status = type("S", (), {"value": "halted"})()
        num_fills = 0
        final_equity = 10_000
        halt_reason = "stopped externally"

    seen: dict[str, Any] = {}

    async def fake_run_shadow(
        request: Any, sm: Any, client: Any, stop: Any = None, funding_client: Any = None
    ) -> Any:
        seen["pair"] = str(request.pair)
        seen["timeframe"] = request.timeframe.value
        seen["strategy"] = request.strategy.id
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
    monkeypatch.setattr(runner_mod, "run_shadow", fake_run_shadow)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "regime-switch",
            "--pair",
            "SOL/EUR",
            "--timeframe",
            "4h",
            "--no-config-from-db",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == {"pair": "SOL/EUR", "timeframe": "4h", "strategy": "regime-switch"}


def test_run_shadow_static_flags_require_all_three() -> None:
    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "regime-switch",
            "--no-config-from-db",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "no-config-from-db" in result.output


def _patch_run_portfolio_shadow(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    import kaupo.core.runner as runner_mod
    import kaupo.data.binance as binance_mod
    import kaupo.data.kraken as kraken_mod

    class FakeResult:
        status = type("S", (), {"value": "halted"})()
        num_fills = 0
        final_equity = 10_000
        halt_reason = "stopped externally"

    async def fake_run_portfolio_shadow(
        request: Any, sm: Any, client: Any, stop: Any = None, funding_client: Any = None
    ) -> Any:
        seen["pairs"] = [str(p) for p in request.pairs]
        seen["timeframe"] = request.timeframe.value
        seen["strategy"] = request.strategy.id
        return FakeResult()

    monkeypatch.setattr(kraken_mod, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(binance_mod, "BinanceClient", _FakeBinanceClient)
    monkeypatch.setattr(runner_mod, "run_portfolio_shadow", fake_run_portfolio_shadow)


def test_run_shadow_pairs_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    _patch_run_portfolio_shadow(monkeypatch, seen)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "momentum-rotation",
            "--pairs",
            "SOL/EUR,BTC/EUR",
            "--timeframe",
            "1h",
            "--no-config-from-db",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Shadow run ended" in result.output
    assert seen == {"pairs": ["BTC/EUR", "SOL/EUR"], "timeframe": "1h", "strategy": "momentum-rotation"}


def test_run_shadow_pairs_requires_no_config_from_db() -> None:
    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "momentum-rotation",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--timeframe",
            "1h",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "--no-config-from-db" in result.output


def test_run_shadow_pair_and_pairs_mutually_exclusive() -> None:
    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "momentum-rotation",
            "--pair",
            "BTC/EUR",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--timeframe",
            "1h",
            "--no-config-from-db",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_run_shadow_pairs_requires_a_portfolio_strategy() -> None:
    result = runner.invoke(
        cli.app,
        [
            "run",
            "shadow",
            "--strategy",
            "regime-switch",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--timeframe",
            "1h",
            "--no-config-from-db",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "not a portfolio strategy" in result.output


def test_run_supervisor_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor starts and stops cleanly with the loop faked out."""
    import kaupo.core.supervisor as supervisor_mod

    captured: dict[str, Any] = {}

    async def fake_run_supervisor(sm: Any, strategies: Any, stop: Any, **kwargs: Any) -> None:
        captured["strategies"] = sorted(strategies)
        captured["kwargs"] = kwargs
        assert stop is not None

    monkeypatch.setattr(supervisor_mod, "run_supervisor", fake_run_supervisor)

    result = runner.invoke(cli.app, ["run", "supervisor", "--strategies-dir", str(EXAMPLES_DIR)])
    assert result.exit_code == 0, result.output
    assert "Supervisor stopped" in result.output
    assert captured["strategies"] == ["momentum-rotation", "regime-switch"]
    assert captured["kwargs"]["reconcile_interval_seconds"] == 15.0


def test_run_supervisor_lint_enforced(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("from time import time\nx = time()\n")
    result = runner.invoke(cli.app, ["run", "supervisor", "--strategies-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "wall-clock" in result.output


def test_backtest_pairs_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.portfolio as pf_mod

    captured: dict[str, Any] = {}

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run(request: Any, sm: Any) -> Any:
        captured["pairs"] = [str(p) for p in request.pairs]
        captured["exchange"] = request.exchange
        metrics = {"num_fills": 2, "universe": captured["pairs"]}
        return RunId("run-2"), FakeResult(), metrics

    monkeypatch.setattr(pf_mod, "run_portfolio_backtest", fake_run)

    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "momentum-rotation",
            "--pairs",
            "SOL/EUR,BTC/EUR",
            "--days",
            "30",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run-2" in result.output
    assert captured["pairs"] == ["BTC/EUR", "SOL/EUR"]  # canonical sorted order
    assert captured["exchange"] == "kraken"


def test_backtest_pair_and_pairs_mutually_exclusive() -> None:
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_backtest_pairs_requires_a_portfolio_strategy() -> None:
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "not a portfolio strategy" in result.output


def test_backtest_pair_requires_a_single_pair_strategy() -> None:
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "momentum-rotation",
            "--pair",
            "BTC/EUR",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "portfolio strategy" in result.output


def test_backtest_pairs_rejects_mixed_quotes() -> None:
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "momentum-rotation",
            "--pairs",
            "BTC/EUR,SOL/USD",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "one quote currency" in result.output


def _capture_backtest_request(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    import kaupo.backtest.run as bt_mod

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run_backtest(request: Any, sm: Any) -> Any:
        captured["risk"] = request.risk
        return RunId("run-1"), FakeResult(), {"num_fills": 0}

    monkeypatch.setattr(bt_mod, "run_backtest", fake_run_backtest)


def test_backtest_risk_override_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaupo.risk.manager import RiskConfig

    captured: dict[str, Any] = {}
    _capture_backtest_request(monkeypatch, captured)

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
            "--max-position-quote",
            "5000",
            "--max-gross-exposure-quote",
            "20000",
            "--max-daily-loss-quote",
            "1500",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    risk = captured["risk"]
    assert isinstance(risk, RiskConfig)
    assert risk.max_position_quote == 5000.0
    assert risk.max_gross_exposure_quote == 20000.0
    assert risk.max_daily_loss_quote == 1500.0
    # the other caps keep the live defaults
    assert risk.min_order_quote == 10.0
    assert risk.max_consecutive_losses == 5


def test_backtest_risk_override_flags_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.backtest.portfolio as pf_mod

    captured: dict[str, Any] = {}

    class FakeResult:
        status = type("S", (), {"value": "completed"})()

    async def fake_run(request: Any, sm: Any) -> Any:
        captured["risk"] = request.risk
        return RunId("run-2"), FakeResult(), {"num_fills": 0}

    monkeypatch.setattr(pf_mod, "run_portfolio_backtest", fake_run)

    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "--strategy",
            "momentum-rotation",
            "--pairs",
            "BTC/EUR,SOL/EUR",
            "--days",
            "30",
            "--max-gross-exposure-quote",
            "100000",
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    risk = captured["risk"]
    assert risk.max_gross_exposure_quote == 100000.0
    assert risk.max_position_quote == 1000.0  # default kept


def test_backtest_risk_defaults_when_no_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaupo.risk.manager import RiskConfig

    captured: dict[str, Any] = {}
    _capture_backtest_request(monkeypatch, captured)

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
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["risk"] == RiskConfig()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-position-quote", "-1"),
        ("--max-gross-exposure-quote", "0"),
        ("--max-daily-loss-quote", "-0.5"),
    ],
)
def test_backtest_risk_override_rejects_non_positive(flag: str, value: str) -> None:
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
            flag,
            value,
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )
    assert result.exit_code == 1
    assert "must be positive" in result.output
