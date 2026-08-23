"""kaupo CLI: ingest data, run backtests, manage strategies, start shadow runs."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from kaupo.config import get_settings
from kaupo.domain import Pair, Timeframe

app = typer.Typer(name="kaupo", help="Kaupo — autonomous algorithmic trading", no_args_is_help=True)
run_app = typer.Typer(help="Start long-running trading loops", no_args_is_help=True)
app.add_typer(run_app, name="run")
console = Console()
err_console = Console(stderr=True)

PairOpt = Annotated[str, typer.Option(help="e.g. BTC/EUR")]
TimeframeOpt = Annotated[str, typer.Option(help="1m 5m 15m 30m 1h 4h 1d")]
DaysOpt = Annotated[int, typer.Option(help="how far back from --end (or now)")]
StartOpt = Annotated[str | None, typer.Option(help="ISO date, overrides --days")]
EndOpt = Annotated[str | None, typer.Option(help="ISO date")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v")]
StrategiesDirOpt = Annotated[Path | None, typer.Option(help="override strategies directory")]
StrategyOpt = Annotated[str, typer.Option(help="strategy id")]
ParamOpt = Annotated[list[str], typer.Option(help="strategy param as key=value (JSON values)")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _parse_params(params: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in params:
        key, sep, raw = p.partition("=")
        if not sep:
            raise typer.BadParameter(f"--param must be key=value, got {p!r}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw  # plain string
    return out


def _range(days: int, start: str | None, end: str | None) -> tuple[datetime, datetime]:
    end_dt = datetime.fromisoformat(end).astimezone(UTC) if end else datetime.now(UTC)
    start_dt = datetime.fromisoformat(start).astimezone(UTC) if start else end_dt - timedelta(days=days)
    return start_dt, end_dt


@app.command()
def ingest(
    pair: PairOpt,
    timeframe: TimeframeOpt = "1h",
    days: DaysOpt = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Download historical candles from the exchange into Postgres."""
    _setup_logging(verbose)
    from kaupo.data.ingest import backfill
    from kaupo.data.kraken import KrakenClient
    from kaupo.db.session import get_sessionmaker

    start_dt, end_dt = _range(days, start, end)
    tf = Timeframe.parse(timeframe)
    p = Pair.parse(pair)

    async def _run() -> int:
        async with KrakenClient() as client:
            return await backfill(client, get_sessionmaker(), p, tf, start_dt, end_dt)

    total = asyncio.run(_run())
    console.print(
        f"[green]Ingested {total} candles[/green] for {p} {tf.value} ({start_dt.date()} → {end_dt.date()})"
    )


@app.command()
def backtest(
    strategy: StrategyOpt,
    pair: PairOpt,
    timeframe: TimeframeOpt = "1h",
    days: DaysOpt = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    param: ParamOpt = [],
    cash: Annotated[float, typer.Option(help="starting quote cash")] = 10_000.0,
    strategies_dir: StrategiesDirOpt = None,
    no_persist: Annotated[bool, typer.Option(help="do not store the run in Postgres")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Backtest a strategy on historical candles."""
    _setup_logging(verbose)
    from kaupo.backtest.run import BacktestRequest, run_backtest
    from kaupo.db.session import get_sessionmaker
    from kaupo.sdk.loader import load_strategies

    settings = get_settings()
    directory = strategies_dir or settings.strategies_dir
    strategies = load_strategies(directory)
    if strategy not in strategies:
        err_console.print(
            f"[red]Unknown strategy {strategy!r}[/red]. Available: {', '.join(sorted(strategies))}"
        )
        raise typer.Exit(1)

    start_dt, end_dt = _range(days, start, end)
    request = BacktestRequest(
        strategy=strategies[strategy],
        params=_parse_params(param),
        pair=Pair.parse(pair),
        timeframe=Timeframe.parse(timeframe),
        start=start_dt,
        end=end_dt,
        starting_cash=cash,
        persist=not no_persist,
    )

    async def _run() -> Any:
        return await run_backtest(request, get_sessionmaker())

    run_id, result, metrics = asyncio.run(_run())
    console.print(f"\n[bold]Backtest run {run_id}[/bold] — status {result.status.value}")
    table = Table(show_header=False)
    for key, value in metrics.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def strategies(strategies_dir: StrategiesDirOpt = None) -> None:
    """List discovered strategies."""
    from kaupo.sdk.loader import load_strategies

    directory = strategies_dir or get_settings().strategies_dir
    loaded = load_strategies(directory)
    table = Table("id", "version", "path")
    for s in loaded.values():
        table.add_row(s.id, s.version, s.path)
    console.print(table)


@app.command(name="lint-strategies")
def lint_strategies(strategies_dir: StrategiesDirOpt = None) -> None:
    """Check strategies for determinism violations (wall-clock, I/O, ...)."""
    from kaupo.sdk.lint import lint_directory

    directory = strategies_dir or get_settings().strategies_dir
    violations = lint_directory(directory)
    if not violations:
        console.print("[green]No violations[/green]")
        return
    for v in violations:
        err_console.print(f"[red]{v}[/red]")
    raise typer.Exit(1)


@run_app.command(name="shadow")
def run_shadow_cmd(
    strategy: StrategyOpt,
    pair: PairOpt,
    timeframe: TimeframeOpt = "1h",
    param: ParamOpt = [],
    cash: Annotated[float, typer.Option(help="virtual starting cash")] = 10_000.0,
    warmup: Annotated[int, typer.Option(help="history candles preloaded from DB")] = 100,
    strategies_dir: StrategiesDirOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Start shadow trading: live market data, virtual money. Ctrl-C to stop."""
    _setup_logging(verbose)
    from kaupo.core.runner import ShadowRequest, run_shadow
    from kaupo.data.kraken import KrakenClient
    from kaupo.db.session import get_sessionmaker
    from kaupo.sdk.loader import load_strategies

    directory = strategies_dir or get_settings().strategies_dir
    strategies = load_strategies(directory)
    if strategy not in strategies:
        err_console.print(
            f"[red]Unknown strategy {strategy!r}[/red]. Available: {', '.join(sorted(strategies))}"
        )
        raise typer.Exit(1)

    request = ShadowRequest(
        strategy=strategies[strategy],
        params=_parse_params(param),
        pair=Pair.parse(pair),
        timeframe=Timeframe.parse(timeframe),
        starting_cash=cash,
        warmup=warmup,
        poll_interval_seconds=get_settings().poll_interval_seconds,
    )

    async def _run() -> Any:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with KrakenClient() as client:
            return await run_shadow(request, get_sessionmaker(), client, stop=stop)

    result = asyncio.run(_run())
    console.print(
        f"[bold]Shadow run ended[/bold] — status {result.status.value}, "
        f"fills {result.num_fills}, final equity {float(result.final_equity):.2f}"
        + (f", reason: {result.halt_reason}" if result.halt_reason else "")
    )
