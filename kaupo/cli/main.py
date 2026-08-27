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
ingest_app = typer.Typer(help="Download historical market data into Postgres", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
run_app = typer.Typer(help="Start long-running trading loops", no_args_is_help=True)
app.add_typer(run_app, name="run")
report_app = typer.Typer(help="Build performance reports", no_args_is_help=True)
app.add_typer(report_app, name="report")
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


def _parse_sweep(sweeps: list[str]) -> dict[str, list[Any]]:
    """--sweep key=v1,v2,v3 into a spec; values parse like --param scalars."""
    out: dict[str, list[Any]] = {}
    for s in sweeps:
        key, sep, raw = s.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--sweep must be key=v1,v2,..., got {s!r}")
        if key in out:
            raise typer.BadParameter(f"--sweep given twice for {key!r}")
        values: list[Any] = []
        for part in raw.split(","):
            try:
                values.append(json.loads(part))
            except json.JSONDecodeError:
                values.append(part)  # plain string
        out[key] = values
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


@ingest_app.command(name="candles")
def ingest_candles(
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


@ingest_app.command(name="funding")
def ingest_funding(
    pair: PairOpt,
    days: Annotated[int, typer.Option(min=1)] = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    exchange: ExchangeOpt = "binance",
    verbose: VerboseOpt = False,
) -> None:
    """Download historical funding rates for the pair's base asset into Postgres.

    Funding comes from the Binance USDT-margined perpetual of the base asset
    (BTC/EUR -> BTC perp). Kraken funding is not supported.
    """
    _setup_logging(verbose)
    from kaupo.data.binance import BinanceClient
    from kaupo.data.funding import FUNDING_EXCHANGE, get_funding_range
    from kaupo.data.ingest import backfill_funding
    from kaupo.db.session import get_sessionmaker

    if exchange != FUNDING_EXCHANGE:
        raise typer.BadParameter(
            f"funding history is only served by {FUNDING_EXCHANGE}; "
            f"--exchange {exchange} is not supported for funding"
        )

    start_dt, end_dt = _range(days, start, end)
    p = Pair.parse(pair)

    async def _run() -> tuple[int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        async with BinanceClient() as client:
            total = await backfill_funding(client, sm, p.base, start_dt, end_dt)
        async with sm() as session:
            first, last, count = await get_funding_range(session, FUNDING_EXCHANGE, p.base)
        return total, first, last, count

    total, first, last, count = asyncio.run(_run())
    console.print(f"[green]Ingested {total} funding rates[/green] for {p.base} from {FUNDING_EXCHANGE}")
    if first is None or last is None:
        return
    console.print(
        f"Database coverage: {count} funding rates, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC"
    )


TRADE_INGEST_MAX_DAYS = 31  # tick volume is large; one run stays bounded


@ingest_app.command(name="trades")
def ingest_trades(
    pair: PairOpt,
    days: Annotated[int, typer.Option(min=1, max=TRADE_INGEST_MAX_DAYS)] = 3,
    start: StartOpt = None,
    end: EndOpt = None,
    exchange: ExchangeOpt = "kraken",
    verbose: VerboseOpt = False,
) -> None:
    """Download recent public trade prints (order flow) for the pair into Postgres.

    Tick volume is large, so the window is capped at 31 days per run. After
    the ingest, the pair's rows older than KAUPO_TRADES_RETENTION_DAYS
    (default 30) are pruned, keeping the table bounded by construction.
    """
    _setup_logging(verbose)
    from kaupo.data.binance import BinanceClient
    from kaupo.data.ingest import backfill_trades
    from kaupo.data.kraken import KrakenClient
    from kaupo.data.trades import get_trade_range, prune_trade_ticks
    from kaupo.db.session import get_sessionmaker

    clients = {"kraken": KrakenClient, "binance": BinanceClient}
    if exchange not in clients:
        raise typer.BadParameter(f"--exchange must be one of: {', '.join(sorted(clients))}")

    start_dt, end_dt = _range(days, start, end)
    if end_dt - start_dt > timedelta(days=TRADE_INGEST_MAX_DAYS):
        raise typer.BadParameter(f"trade ingest is capped at {TRADE_INGEST_MAX_DAYS} days per run")
    p = Pair.parse(pair)
    retention_days = get_settings().trades_retention_days

    async def _run() -> tuple[int, int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        async with clients[exchange]() as client:
            total = await backfill_trades(client, sm, p, start_dt, end_dt)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with sm() as session:
            pruned = await prune_trade_ticks(session, exchange, str(p), cutoff)
            await session.commit()
            first, last, count = await get_trade_range(session, exchange, str(p))
        return total, pruned, first, last, count

    total, pruned, first, last, count = asyncio.run(_run())
    console.print(f"[green]Ingested {total} trade ticks[/green] for {p} from {exchange}")
    if first is not None and last is not None:
        console.print(
            f"Database coverage: {count} trade ticks, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC"
        )
    console.print(f"Pruned {pruned} trade ticks older than {retention_days} days")


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
    sweep: Annotated[
        list[str],
        typer.Option(
            help="sweep a strategy param: key=v1,v2,v3 (repeatable, <=50 points; "
            "not with --stability-windows)"
        ),
    ] = [],
    cash: Annotated[float, typer.Option(help="starting quote cash", min=0.01)] = 10_000.0,
    exchange: ExchangeOpt = "kraken",
    stability_windows: Annotated[
        int | None,
        typer.Option(
            help="also run K equal time slices of the window and print per-window metrics",
            min=2,
            max=12,
        ),
    ] = None,
    max_position_quote: Annotated[
        float | None, typer.Option(help="research override: per-pair position cap (quote)")
    ] = None,
    max_gross_exposure_quote: Annotated[
        float | None, typer.Option(help="research override: total exposure cap (quote)")
    ] = None,
    max_daily_loss_quote: Annotated[
        float | None, typer.Option(help="research override: daily loss halt (quote)")
    ] = None,
    strategies_dir: StrategiesDirOpt = None,
    no_persist: Annotated[bool, typer.Option(help="do not store the run in Postgres")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Backtest a strategy on historical candles. Pass --pair or --pairs."""
    _setup_logging(verbose)
    from dataclasses import replace

    from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
    from kaupo.backtest.run import BacktestRequest, backtest_risk_config, run_backtest
    from kaupo.backtest.stability import run_stability_slices, stability_marker
    from kaupo.backtest.sweep import run_sweep, validate_sweep_keys, validate_sweep_spec
    from kaupo.db.session import get_sessionmaker
    from kaupo.domain import new_id
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

    base_params = _parse_params(param)
    if sweep and stability_windows is not None:
        err_console.print("[red]--sweep cannot be combined with --stability-windows[/red]")
        raise typer.Exit(1)
    sweep_spec = _parse_sweep(sweep) if sweep else None
    if sweep_spec is not None:
        try:
            validate_sweep_spec(sweep_spec)
            validate_sweep_keys(loaded, base_params, sweep_spec)
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    start_dt, end_dt = _range(days, start, end)
    request: BacktestRequest | PortfolioBacktestRequest
    try:
        risk = backtest_risk_config(
            max_position_quote=max_position_quote,
            max_gross_exposure_quote=max_gross_exposure_quote,
            max_daily_loss_quote=max_daily_loss_quote,
        )
        if pair is not None and pairs is None:
            if loaded.is_portfolio:
                err_console.print(f"[red]Strategy {strategy!r} is a portfolio strategy[/red] — use --pairs")
                raise typer.Exit(1)
            request = BacktestRequest(
                strategy=loaded,
                params=base_params,
                pair=Pair.parse(pair),
                timeframe=Timeframe.parse(timeframe),
                start=start_dt,
                end=end_dt,
                starting_cash=cash,
                exchange=exchange,
                risk=risk,
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
                params=base_params,
                pairs=[Pair.parse(p) for p in pairs.split(",")],
                timeframe=Timeframe.parse(timeframe),
                start=start_dt,
                end=end_dt,
                starting_cash=cash,
                exchange=exchange,
                risk=risk,
                persist=not no_persist,
            )
        else:
            err_console.print("[red]Pass exactly one of --pair or --pairs[/red]")
            raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    # one group id ties a stability or sweep run set together
    group = new_id() if stability_windows is not None or sweep_spec is not None else None
    if stability_windows is not None:
        assert group is not None
        request = replace(request, stability=stability_marker(group, "full", stability_windows))

    if sweep_spec is not None:
        assert group is not None

        async def _run_sweep() -> Any:
            sm = get_sessionmaker()
            return await run_sweep(request, sm, group=group, spec=sweep_spec)

        first_run_id, aggregation = asyncio.run(_run_sweep())
        entries = aggregation["sweep"]
        header = f"\n[bold]Sweep {group}[/bold] — {len(entries)} points"
        if first_run_id is not None:
            header += f" (first run {first_run_id})"
        console.print(header)
        surface = Table(*sweep_spec, "sharpe", "max DD", "return", "trips")
        for entry in entries:
            cells = [str(entry["params"][key]) for key in sweep_spec]
            if "error" in entry:
                surface.add_row(*cells, f"[red]error: {entry['error']}[/red]")
                continue
            m = entry["metrics"]
            surface.add_row(
                *cells,
                str(m.get("sharpe", "—")),
                str(m.get("max_drawdown_pct", "—")),
                str(m.get("total_return_pct", "—")),
                str(m.get("num_round_trips", "—")),
            )
        console.print(surface)
        return

    async def _run() -> Any:
        sm = get_sessionmaker()
        if isinstance(request, PortfolioBacktestRequest):
            run_id, result, metrics = await run_portfolio_backtest(request, sm)
        else:
            run_id, result, metrics = await run_backtest(request, sm)
        stability = None
        if group is not None and stability_windows is not None:
            stability = await run_stability_slices(request, sm, group=group, windows=stability_windows)
        return run_id, result, metrics, stability

    run_id, result, metrics, stability = asyncio.run(_run())
    console.print(f"\n[bold]Backtest run {run_id}[/bold] — status {result.status.value}")
    table = Table(show_header=False)
    for key, value in metrics.items():
        table.add_row(key, str(value))
    console.print(table)

    if stability is not None:
        console.print(f"\n[bold]Stability windows[/bold] — group {group}")
        stable = Table("window", "start", "end", "sharpe", "max DD", "return", "trips")
        for entry in stability["slices"]:
            start, end = entry["start"][:16], entry["end"][:16]
            if "error" in entry:
                stable.add_row(str(entry["window"]), start, end, f"[red]error: {entry['error']}[/red]")
                continue
            m = entry["metrics"]
            stable.add_row(
                str(entry["window"]),
                start,
                end,
                str(m.get("sharpe", "—")),
                str(m.get("max_drawdown_pct", "—")),
                str(m.get("total_return_pct", "—")),
                str(m.get("num_round_trips", "—")),
            )
        console.print(stable)


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
    pairs: Annotated[
        str | None,
        typer.Option(
            help="comma-separated universe for a portfolio shadow run, e.g. BTC/EUR,SOL/EUR "
            "(requires --no-config-from-db)"
        ),
    ] = None,
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
    A portfolio run (--pairs) is always static: the settings table holds one
    pair only.
    """
    _setup_logging(verbose)
    from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest, run_portfolio_shadow, run_shadow
    from kaupo.data.binance import BinanceClient
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
        if pairs is not None:
            # a portfolio run is always static: settings hold one pair only
            if pair is not None:
                err_console.print("[red]Pass exactly one of --pair or --pairs[/red]")
                raise typer.Exit(1)
            if not no_config_from_db:
                err_console.print("[red]--pairs requires --no-config-from-db[/red]")
                raise typer.Exit(1)
            if strategy is None or timeframe is None:
                err_console.print("[red]--pairs requires --strategy and --timeframe[/red]")
                raise typer.Exit(1)
            if strategy not in strategies:
                err_console.print(
                    f"[red]Unknown strategy {strategy!r}[/red]. Available: {', '.join(sorted(strategies))}"
                )
                raise typer.Exit(1)
            if not strategies[strategy].is_portfolio:
                err_console.print(
                    f"[red]Strategy {strategy!r} is not a portfolio strategy[/red] — use --pair"
                )
                raise typer.Exit(1)
            try:
                portfolio_request = PortfolioShadowRequest(
                    strategy=strategies[strategy],
                    params=_parse_params(param),
                    pairs=[Pair.parse(p) for p in pairs.split(",")],
                    timeframe=Timeframe.parse(timeframe),
                    starting_cash=cash,
                    warmup=warmup,
                    poll_interval_seconds=get_settings().poll_interval_seconds,
                    funding_refresh_seconds=get_settings().funding_refresh_seconds,
                )
            except ValueError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            console.print(
                f"Starting portfolio shadow run: {strategy} on {portfolio_request.pairs} {timeframe}"
            )
            async with KrakenClient() as client, BinanceClient() as funding_client:
                return await run_portfolio_shadow(
                    portfolio_request, sessionmaker, client, stop=stop, funding_client=funding_client
                )
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
            funding_refresh_seconds=get_settings().funding_refresh_seconds,
        )
        console.print(f"Starting shadow run: {resolved.strategy} on {resolved.pair} {resolved.timeframe}")
        async with KrakenClient() as client, BinanceClient() as funding_client:
            return await run_shadow(request, sessionmaker, client, stop=stop, funding_client=funding_client)

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
            run_funding_refresh_seconds=settings.funding_refresh_seconds,
        )

    asyncio.run(_run())
    console.print("[bold]Supervisor stopped[/bold]")


@run_app.command(name="backtest-worker")
def run_backtest_worker_cmd(
    poll_interval: Annotated[float, typer.Option(help="seconds between queue polls", min=0.1)] = 5.0,
    verbose: VerboseOpt = False,
) -> None:
    """Execute queued backtest jobs from the database. Ctrl-C to stop.

    The API enqueues a job per POST /api/v1/backtests; this worker claims
    the oldest queued one, runs it, and stores the result. Jobs wait
    queued while no worker runs, and survive restarts of either process.
    """
    _setup_logging(verbose)
    from kaupo.core.backtest_worker import run_backtest_worker
    from kaupo.db.session import get_sessionmaker

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_backtest_worker(
            get_sessionmaker(),
            get_settings(),
            stop,
            poll_interval_seconds=poll_interval,
        )

    asyncio.run(_run())
    console.print("[bold]Backtest worker stopped[/bold]")


@run_app.command(name="book-collector")
def run_book_collector_cmd(
    verbose: VerboseOpt = False,
) -> None:
    """Collect top-of-book snapshots for the pair-quality universe. Ctrl-C to stop.

    Each cycle polls the best bid/ask (with sizes) of every universe pair on
    Kraken and stores one row per observation. Rows older than
    KAUPO_BOOK_RETENTION_DAYS (default 30) are pruned after each cycle. No
    public API serves historical books, so collection is forward only.
    """
    _setup_logging(verbose)
    from kaupo.core.book_collector import run_book_collector
    from kaupo.data.kraken import KrakenClient
    from kaupo.db.session import get_sessionmaker

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with KrakenClient() as client:
            await run_book_collector(get_sessionmaker(), get_settings(), stop, client=client)

    asyncio.run(_run())
    console.print("[bold]Book collector stopped[/bold]")


@report_app.command(name="rolling-origin")
def report_rolling_origin(
    days: Annotated[int, typer.Option(help="window length in days", min=1)] = 30,
    strategies_dir: StrategiesDirOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Re-backtest every enabled shadow assignment over the last --days days.

    Compares each assignment's fresh backtest with its shadow chain's actual
    equity and fills over the same window, prints the digest table, persists
    one row per ISO week, and pushes the digest to the ntfy topic (no-op
    without a configured topic).
    """
    _setup_logging(verbose)
    from kaupo.db.session import get_sessionmaker
    from kaupo.report.rolling import build_rolling_origin_report, send_digest

    async def _run() -> Any:
        sessionmaker = get_sessionmaker()
        body = await build_rolling_origin_report(sessionmaker, days=days, strategies_dir=strategies_dir)
        await send_digest(body)
        return body

    body = asyncio.run(_run())
    console.print(
        f"\n[bold]Rolling-origin report {body['period']}[/bold] — window {body['window_days']} days"
    )
    table = Table("assignment", "strategy", "pair(s)", "tf", "backtest", "shadow", "verdict")
    for entry in body["assignments"]:
        if "error" in entry:
            cells = (f"[red]error: {entry['error']}[/red]", "", entry["verdict"])
        elif "note" in entry["shadow"]:
            cells = ("", f"[yellow]{entry['shadow']['note']}[/yellow]", entry["verdict"])
        else:
            backtest, shadow = entry["backtest"], entry["shadow"]
            cells = (
                f"sharpe {backtest['sharpe']} ({backtest['num_round_trips']} trips)",
                f"sharpe {shadow['sharpe']} ({shadow['num_fills']} fills)",
                entry["verdict"],
            )
        table.add_row(entry["id"], entry["strategy_id"], entry["pair"], entry["timeframe"], *cells)
    console.print(table)
