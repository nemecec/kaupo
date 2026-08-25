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
ExchangeOpt = Annotated[str, typer.Option(help="candle data source: kraken or binance")]
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


def _parse_dt(value: str) -> datetime:
    """ISO datetime; naive input is treated as UTC (never host-local)."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _range(days: int, start: str | None, end: str | None) -> tuple[datetime, datetime]:
    end_dt = _parse_dt(end) if end else datetime.now(UTC)
    start_dt = _parse_dt(start) if start else end_dt - timedelta(days=days)
    return start_dt, end_dt


def _ensure_strategies_clean(directory: Path) -> None:
    """Refuse to run strategies with determinism violations."""
    from kaupo.sdk.lint import lint_directory

    violations = lint_directory(directory)
    if violations:
        for v in violations:
            err_console.print(f"[red]{v}[/red]")
        raise typer.Exit(1)


@app.command()
def ingest(
    pair: PairOpt,
    timeframe: TimeframeOpt = "1h",
    days: Annotated[int, typer.Option(min=1)] = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    exchange: ExchangeOpt = "kraken",
    verbose: VerboseOpt = False,
) -> None:
    """Download historical candles from the exchange into Postgres."""
    _setup_logging(verbose)
    from kaupo.data.binance import BinanceClient
    from kaupo.data.candles import get_candle_range
    from kaupo.data.ingest import backfill
    from kaupo.data.kraken import KrakenClient
    from kaupo.db.session import get_sessionmaker

    clients = {"kraken": KrakenClient, "binance": BinanceClient}
    if exchange not in clients:
        raise typer.BadParameter(f"--exchange must be one of: {', '.join(sorted(clients))}")

    start_dt, end_dt = _range(days, start, end)
    tf = Timeframe.parse(timeframe)
    p = Pair.parse(pair)

    async def _run() -> tuple[int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        async with clients[exchange]() as client:
            total = await backfill(client, sm, p, tf, start_dt, end_dt)
        async with sm() as session:
            first, last, count = await get_candle_range(session, p, tf, exchange=exchange)
        return total, first, last, count

    total, first, last, count = asyncio.run(_run())
    console.print(f"[green]Ingested {total} candles[/green] for {p} {tf.value} from {exchange}")
    if first is None or last is None:
        return
    console.print(f"Database coverage: {count} candles, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC")
    if first > start_dt:
        warning = (
            f"[yellow]Coverage starts {first:%Y-%m-%d}, after the requested {start_dt:%Y-%m-%d}.[/yellow]"
        )
        if exchange == "kraken":
            warning += " Kraken serves at most the 720 newest candles of a timeframe."
        console.print(warning)


@app.command()
def backtest(
    strategy: StrategyOpt,
    pair: Annotated[str | None, typer.Option(help="e.g. BTC/EUR")] = None,
    pairs: Annotated[
        str | None,
        typer.Option(help="comma-separated universe for a portfolio backtest, e.g. BTC/EUR,SOL/EUR"),
    ] = None,
    timeframe: TimeframeOpt = "1h",
    days: Annotated[int, typer.Option(min=1)] = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    param: ParamOpt = [],
    cash: Annotated[float, typer.Option(help="starting quote cash", min=0.01)] = 10_000.0,
    exchange: ExchangeOpt = "kraken",
    strategies_dir: StrategiesDirOpt = None,
    no_persist: Annotated[bool, typer.Option(help="do not store the run in Postgres")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Backtest a strategy on historical candles. Pass --pair or --pairs."""
    _setup_logging(verbose)
    from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
    from kaupo.backtest.run import BacktestRequest, run_backtest
    from kaupo.db.session import get_sessionmaker
    from kaupo.sdk.loader import load_strategies

    settings = get_settings()
    directory = strategies_dir or settings.strategies_dir
    _ensure_strategies_clean(directory)
    strategies = load_strategies(directory)
    if strategy not in strategies:
        err_console.print(
            f"[red]Unknown strategy {strategy!r}[/red]. Available: {', '.join(sorted(strategies))}"
        )
        raise typer.Exit(1)
    loaded = strategies[strategy]

    start_dt, end_dt = _range(days, start, end)
    request: BacktestRequest | PortfolioBacktestRequest
    try:
        if pair is not None and pairs is None:
            if loaded.is_portfolio:
                err_console.print(f"[red]Strategy {strategy!r} is a portfolio strategy[/red] — use --pairs")
                raise typer.Exit(1)
            request = BacktestRequest(
                strategy=loaded,
                params=_parse_params(param),
                pair=Pair.parse(pair),
                timeframe=Timeframe.parse(timeframe),
                start=start_dt,
                end=end_dt,
                starting_cash=cash,
                exchange=exchange,
                persist=not no_persist,
            )
        elif pairs is not None and pair is None:
            if not loaded.is_portfolio:
                err_console.print(
                    f"[red]Strategy {strategy!r} is not a portfolio strategy[/red] — use --pair"
                )
                raise typer.Exit(1)
            request = PortfolioBacktestRequest(
                strategy=loaded,
                params=_parse_params(param),
                pairs=[Pair.parse(p) for p in pairs.split(",")],
                timeframe=Timeframe.parse(timeframe),
                start=start_dt,
                end=end_dt,
                starting_cash=cash,
                exchange=exchange,
                persist=not no_persist,
            )
        else:
            err_console.print("[red]Pass exactly one of --pair or --pairs[/red]")
            raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    async def _run() -> Any:
        if isinstance(request, PortfolioBacktestRequest):
            return await run_portfolio_backtest(request, get_sessionmaker())
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
    strategy: Annotated[
        str | None, typer.Option(help="strategy id (seeds the DB; stored setting wins)")
    ] = None,
    pair: Annotated[str | None, typer.Option(help="e.g. BTC/EUR (seeds the DB; stored setting wins)")] = None,
    timeframe: Annotated[
        str | None, typer.Option(help="1m 5m 15m 30m 1h 4h 1d (seeds the DB; stored setting wins)")
    ] = None,
    param: ParamOpt = [],
    cash: Annotated[float, typer.Option(help="virtual starting cash", min=0.01)] = 10_000.0,
    warmup: Annotated[
        int | None, typer.Option(help="history candles preloaded from DB (default: lookback)")
    ] = None,
    no_config_from_db: Annotated[
        bool,
        typer.Option(
            help="use the flags as given; do not read or seed the settings table (static side runs)"
        ),
    ] = False,
    strategies_dir: StrategiesDirOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Start shadow trading: live market data, virtual money. Ctrl-C to stop.

    Strategy, pair, and timeframe resolve from the settings table; the flags
    only seed a fresh database. Change them at runtime with PUT /api/v1/settings.
    """
    _setup_logging(verbose)
    from kaupo.core.runner import ShadowRequest, run_shadow
    from kaupo.data.kraken import KrakenClient
    from kaupo.data.settings import resolve_shadow_config
    from kaupo.db.session import get_sessionmaker, sm_scope
    from kaupo.sdk.loader import load_strategies

    directory = strategies_dir or get_settings().strategies_dir
    _ensure_strategies_clean(directory)
    strategies = load_strategies(directory)

    async def _run() -> Any:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        sessionmaker = get_sessionmaker()
        if no_config_from_db:
            if strategy is None or pair is None or timeframe is None:
                err_console.print(
                    "[red]--no-config-from-db requires --strategy, --pair, and --timeframe[/red]"
                )
                raise typer.Exit(1)
            from kaupo.data.settings import ShadowSettings

            resolved = ShadowSettings(strategy=strategy, pair=pair, timeframe=timeframe)
        else:
            async with sm_scope(sessionmaker) as session:
                resolved = await resolve_shadow_config(session, strategy, pair, timeframe)
        if resolved.strategy not in strategies:
            err_console.print(
                f"[red]Unknown strategy {resolved.strategy!r}[/red]. "
                f"Available: {', '.join(sorted(strategies))}"
            )
            raise typer.Exit(1)
        request = ShadowRequest(
            strategy=strategies[resolved.strategy],
            params=_parse_params(param),
            pair=Pair.parse(resolved.pair),
            timeframe=Timeframe.parse(resolved.timeframe),
            starting_cash=cash,
            warmup=warmup,
            poll_interval_seconds=get_settings().poll_interval_seconds,
        )
        console.print(f"Starting shadow run: {resolved.strategy} on {resolved.pair} {resolved.timeframe}")
        async with KrakenClient() as client:
            return await run_shadow(request, sessionmaker, client, stop=stop)

    result = asyncio.run(_run())
    console.print(
        f"[bold]Shadow run ended[/bold] — status {result.status.value}, "
        f"fills {result.num_fills}, final equity {float(result.final_equity):.2f}"
        + (f", reason: {result.halt_reason}" if result.halt_reason else "")
    )


@run_app.command(name="supervisor")
def run_supervisor_cmd(
    strategies_dir: StrategiesDirOpt = None,
    reconcile_interval: Annotated[float, typer.Option(help="seconds between reconcile passes", min=1)] = 15.0,
    verbose: VerboseOpt = False,
) -> None:
    """Reconcile shadow runs to the run_assignments table. Ctrl-C to stop.

    Enabled rows are the desired state: the supervisor starts missing runs,
    stops disabled or changed ones, and restarts crashes with a backoff.
    Manage the rows through /api/v1/assignments.
    """
    _setup_logging(verbose)
    from kaupo.core.supervisor import run_supervisor
    from kaupo.db.session import get_sessionmaker
    from kaupo.sdk.loader import load_strategies

    settings = get_settings()
    directory = strategies_dir or settings.strategies_dir
    _ensure_strategies_clean(directory)
    strategies = load_strategies(directory)

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_supervisor(
            get_sessionmaker(),
            strategies,
            stop,
            reconcile_interval_seconds=reconcile_interval,
            run_poll_interval_seconds=settings.poll_interval_seconds,
        )

    asyncio.run(_run())
    console.print("[bold]Supervisor stopped[/bold]")
