"""Trading schedule wiring for backtest inputs and request plans."""

from dataclasses import replace
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.backtest.run import BacktestRequest
from kaupo.cli.main import _load_trading_schedule, app
from kaupo.domain import RunId
from kaupo.market_calendar import TradingSchedule

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"
runner = CliRunner()
SCHEDULE = {
    "timezone": "UTC",
    "week": {"0": [{"start": 0, "end": 1440}]},
}


def test_backtest_schema_validates_and_round_trips_schedule() -> None:
    body = BacktestIn(strategy="regime-switch", pair="BTC/EUR", trading_schedule=SCHEDULE)

    assert isinstance(body.trading_schedule, TradingSchedule)
    assert BacktestIn.model_validate(body.model_dump(mode="json")) == body


def test_backtest_schema_rejects_invalid_schedule() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            trading_schedule={"timezone": "not/a/timezone", "week": {"0": [{"start": 0, "end": 1}]}},
        )


def test_cli_loads_schedule_only_from_explicit_json_path(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    path.write_text('{"timezone":"UTC","week":{"0":[{"start":0,"end":1440}]}}')

    assert _load_trading_schedule(path) == TradingSchedule.model_validate(SCHEDULE)
    assert _load_trading_schedule(None) is None


@pytest.mark.parametrize(
    ("contents", "message"),
    [("{", "valid JSON"), ('{"timezone":"bad","week":{}}', "invalid")],
)
def test_cli_rejects_bad_schedule_file(tmp_path: Path, contents: str, message: str) -> None:
    path = tmp_path / "schedule.json"
    path.write_text(contents)

    with pytest.raises(typer.BadParameter, match=message):
        _load_trading_schedule(path)


def test_cli_backtest_passes_schedule_to_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kaupo.backtest.run as run_module

    path = tmp_path / "schedule.json"
    path.write_text('{"timezone":"UTC","week":{"0":[{"start":0,"end":1440}]}}')
    captured: dict[str, object] = {}

    class FakeResult:
        status = type("Status", (), {"value": "completed"})()

    async def fake_run(request: object, sessionmaker: object) -> object:
        captured["schedule"] = request.trading_schedule  # type: ignore[attr-defined]
        return RunId("run-1"), FakeResult(), {"num_fills": 0}

    monkeypatch.setattr(run_module, "run_backtest", fake_run)
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "regime-switch",
            "--pair",
            "BTC/EUR",
            "--trading-schedule",
            str(path),
            "--strategies-dir",
            str(EXAMPLES_DIR),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["schedule"] == TradingSchedule.model_validate(SCHEDULE)


def test_build_backtest_request_propagates_schedule_to_single_pair() -> None:
    body = BacktestIn(strategy="regime-switch", pair="BTC/EUR", trading_schedule=SCHEDULE)
    request = build_backtest_request(body, lint_and_load_strategies(EXAMPLES_DIR))

    assert isinstance(request, BacktestRequest)
    assert request.trading_schedule == body.trading_schedule

    stability_request = replace(request, stability={"window": 0})
    assert stability_request.trading_schedule == body.trading_schedule
    sweep_request = replace(request, sweep={"point": 0})
    assert sweep_request.trading_schedule == body.trading_schedule


def test_build_backtest_request_propagates_schedule_to_portfolio() -> None:
    body = BacktestIn(
        strategy="momentum-rotation",
        pairs=["BTC/EUR", "SOL/EUR"],
        trading_schedule=SCHEDULE,
    )
    request = build_backtest_request(body, lint_and_load_strategies(EXAMPLES_DIR))

    assert isinstance(request, PortfolioBacktestRequest)
    assert request.trading_schedule == body.trading_schedule
