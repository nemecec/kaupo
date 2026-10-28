"""kaupo CLI: ingest data, run backtests, manage strategies, start shadow runs."""

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from kaupo.config import get_settings
from kaupo.domain import Pair, Timeframe
from kaupo.market_calendar import TradingSchedule

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


def _load_trading_schedule(path: Path | None) -> TradingSchedule | None:
    """Load and validate a schedule from the explicitly supplied JSON path."""
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise typer.BadParameter(f"--trading-schedule cannot read {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--trading-schedule must be valid JSON: {exc.msg}") from None
    try:
        return TradingSchedule.model_validate(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"--trading-schedule is invalid: {exc}") from None


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


@ingest_app.command(name="xstocks-markets")
def ingest_xstocks_markets(
    country: Annotated[str | None, typer.Option(help="Optional ISO country filter, e.g. EE")] = None,
) -> None:
    """List public Kraken xStocks markets. No account credentials are required."""
    from kaupo.data.xstocks import XStocksClient

    async def _run() -> Any:
        async with XStocksClient() as client:
            return await client.markets(country)

    table = Table("pair", "Kraken symbol", "status", "price tick")
    for market in asyncio.run(_run()):
        table.add_row(str(market.pair), market.api_pair, market.status, market.tick_size)
    console.print(table)


@run_app.command(name="xstocks-balances")
def xstocks_balances(
    units: Annotated[str, typer.Option(help="Required xStocks units: rebased or base")],
) -> None:
    """Read default-wallet balances with explicit xStocks units and original asset IDs."""
    from kaupo.venues.xstocks_account import read_balances

    if units not in ("rebased", "base"):
        raise typer.BadParameter("--units must be rebased or base")
    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise typer.BadParameter("Kraken API credentials are missing")
    started_at = datetime.now(UTC)
    try:
        balances = asyncio.run(
            read_balances(settings.kraken_api_key, settings.kraken_api_secret, units=units)
        )
    except ValueError as exc:
        err_console.print(f"Balance read failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "status": "balances_read",
                "wallet": "default",
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "rebase_multiplier": units,
                "balances": balances,
            }
        )
    )


@run_app.command(name="xstocks-open-orders")
def xstocks_open_orders(
    units: Annotated[str, typer.Option(help="Required xStocks units: rebased or base")],
) -> None:
    """Read default-wallet open orders with explicit xStocks units and original symbols."""
    from kaupo.venues.xstocks_account import read_open_orders

    if units not in ("rebased", "base"):
        raise typer.BadParameter("--units must be rebased or base")
    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise typer.BadParameter("Kraken API credentials are missing")
    started_at = datetime.now(UTC)
    try:
        open_orders = asyncio.run(
            read_open_orders(settings.kraken_api_key, settings.kraken_api_secret, units=units)
        )
    except ValueError as exc:
        err_console.print(f"Open-order read failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "status": "open_orders_read",
                "wallet": "default",
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "rebase_multiplier": units,
                "open_orders": open_orders,
            }
        )
    )


@run_app.command(name="xstocks-order-status")
def xstocks_order_status(
    units: Annotated[str, typer.Option(help="Required xStocks units: rebased or base")],
    order_id: Annotated[list[str], typer.Option(help="Exact Kraken order ID; repeat for up to 50 IDs")],
    expected_expiry: Annotated[
        int | None, typer.Option(help="Assert an absolute Unix expiry for one order")
    ] = None,
) -> None:
    """Read specific orders, including closed, canceled, and expired orders."""
    from kaupo.venues.xstocks_expiry_check import check_expiry_request, check_order_expiry
    from kaupo.venues.xstocks_order_status import check_order_request, read_order_status

    try:
        check_order_request(order_id, units)
        if expected_expiry is not None:
            check_expiry_request(order_id, expected_expiry)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise typer.BadParameter("Kraken API credentials are missing")
    started_at = datetime.now(UTC)
    try:
        orders = asyncio.run(
            read_order_status(
                settings.kraken_api_key, settings.kraken_api_secret, units=units, order_ids=order_id
            )
        )
    except ValueError as exc:
        err_console.print(f"Order-status read failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    completed_at = datetime.now(UTC)
    expiry_check = (
        check_order_expiry(
            orders[order_id[0]],
            expected_expiry=expected_expiry,
            started_at=started_at,
            completed_at=completed_at,
        )
        if expected_expiry is not None
        else None
    )
    typer.echo(
        json.dumps(
            {
                "status": "order_status_read",
                "wallet": "default",
                "rebase_multiplier": units,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "requested_order_ids": order_id,
                "orders": orders,
                **({"expiry_check": expiry_check} if expiry_check is not None else {}),
            }
        )
    )

    if expiry_check is not None and expiry_check["status"] == "needs_review":
        raise typer.Exit(1)


@run_app.command(name="xstocks-fills")
def xstocks_fills(
    units: Annotated[str, typer.Option(help="Required xStocks units: rebased or base")],
    start: Annotated[int, typer.Option(help="Exclusive start as Unix seconds")],
    end: Annotated[int, typer.Option(help="Inclusive end as Unix seconds (maximum window: 31 days)")],
    include_ledgers: Annotated[bool, typer.Option(help="Fetch all referenced ledger entries")] = False,
    map_assets: Annotated[
        bool, typer.Option(help="Map exact pair and asset IDs with current public metadata")
    ] = False,
    check_ledgers: Annotated[
        bool, typer.Option(help="Check principal movements; includes ledgers and mapping")
    ] = False,
    check_balance_links: Annotated[
        bool, typer.Option(help="Check selected ledger balance links; includes ledgers")
    ] = False,
) -> None:
    """Read default-wallet fill history with explicit xStocks units and original symbols."""
    from kaupo.venues.xstocks_fills import check_fill_window, read_fills

    if units not in ("rebased", "base"):
        raise typer.BadParameter("--units must be rebased or base")
    try:
        check_fill_window(start, end)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise typer.BadParameter("Kraken API credentials are missing")
    include_ledgers = include_ledgers or check_ledgers or check_balance_links
    map_assets = map_assets or check_ledgers
    started_at = datetime.now(UTC)

    async def _read() -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        from kaupo.venues.xstocks_ledgers import read_fill_ledgers

        fills = await read_fills(
            settings.kraken_api_key, settings.kraken_api_secret, units=units, start=start, end=end
        )
        ledgers = None
        if include_ledgers:
            ledgers = await read_fill_ledgers(
                settings.kraken_api_key, settings.kraken_api_secret, units=units, fills=fills
            )
        identities = None
        if map_assets:
            from kaupo.data.xstocks import XStocksClient
            from kaupo.data.xstocks_identity import map_account_identities

            async with XStocksClient() as public:
                metadata = await public.identity_metadata()
            identities = map_account_identities(metadata, fills, ledgers)
        return fills, ledgers, identities

    try:
        fills, ledgers, identities = asyncio.run(_read())
    except ValueError as exc:
        err_console.print(f"Fill read failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    ledger_check = None
    if check_ledgers:
        from kaupo.venues.xstocks_ledger_check import check_fill_ledgers

        assert ledgers is not None and identities is not None
        ledger_check = check_fill_ledgers(fills, ledgers, identities)
    balance_links = None
    if check_balance_links:
        from kaupo.venues.xstocks_balance_links import check_balance_links as check_links

        assert ledgers is not None
        balance_links = check_links(ledgers)
    typer.echo(
        json.dumps(
            {
                "status": "fills_and_ledgers_read" if include_ledgers else "fills_read",
                "wallet": "default",
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "rebase_multiplier": units,
                "start_exclusive": start,
                "end_inclusive": end,
                "consolidate_taker": False,
                "fills": fills,
                **({"ledgers": ledgers} if include_ledgers else {}),
                **({"identities": identities} if map_assets else {}),
                **({"ledger_check": ledger_check} if check_ledgers else {}),
                **({"balance_links": balance_links} if check_balance_links else {}),
            }
        )
    )
    if any(
        report is not None and report["status"] == "needs_review" for report in (ledger_check, balance_links)
    ):
        raise typer.Exit(1)


@run_app.command(name="xstocks-ledgers")
def xstocks_ledgers(
    asset: Annotated[str, typer.Option(help="Exact Kraken asset ID")],
    asset_class: Annotated[str, typer.Option(help="currency or tokenized_asset")],
    units: Annotated[str, typer.Option(help="rebased or base")],
    start: Annotated[int, typer.Option(help="Exclusive start as Unix seconds")],
    end: Annotated[int, typer.Option(help="Inclusive end as Unix seconds; maximum window 31 days")],
    check_balance_links: Annotated[
        bool, typer.Option(help="Check balance links in the returned window")
    ] = False,
    opening_balance: Annotated[
        str | None, typer.Option(help="Balance at the exclusive start; requires closing balance")
    ] = None,
    closing_balance: Annotated[
        str | None, typer.Option(help="Balance at the inclusive end; requires opening balance")
    ] = None,
) -> None:
    """Read all ledger types for one asset in the default wallet."""
    from kaupo.venues.xstocks_balance_links import check_balance_links as check_links
    from kaupo.venues.xstocks_balance_links import check_window_balances
    from kaupo.venues.xstocks_ledger_check import _decimal
    from kaupo.venues.xstocks_ledger_history import check_ledger_request, read_ledger_history

    try:
        check_ledger_request(asset, asset_class, units, start, end)
        if (opening_balance is None) != (closing_balance is None):
            raise ValueError("Opening and closing balances must be supplied together")
        if opening_balance is not None:
            _decimal(opening_balance)
            _decimal(closing_balance)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise typer.BadParameter("Kraken API credentials are missing")
    started_at = datetime.now(UTC)
    try:
        entries = asyncio.run(
            read_ledger_history(
                settings.kraken_api_key,
                settings.kraken_api_secret,
                units=units,
                start=start,
                end=end,
                asset=asset,
                asset_class=asset_class,
            )
        )
    except ValueError as exc:
        err_console.print(f"Ledger history read failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    report = check_links(entries) if check_balance_links else None
    if report is not None:
        report["scope"] = "asset_window"
    window_report = (
        check_window_balances(
            entries, asset=asset, asset_class=asset_class, opening=opening_balance, closing=closing_balance
        )
        if opening_balance is not None and closing_balance is not None
        else None
    )
    typer.echo(
        json.dumps(
            {
                "status": "ledger_history_read",
                "wallet": "default",
                "asset": asset,
                "asset_class": asset_class,
                "rebase_multiplier": units,
                "start_exclusive": start,
                "end_inclusive": end,
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "ledgers": entries,
                **({"balance_window": window_report} if window_report is not None else {}),
                **({"balance_links": report} if report is not None else {}),
            }
        )
    )
    if any(r is not None and r["status"] == "needs_review" for r in (report, window_report)):
        raise typer.Exit(1)


@run_app.command(name="xstocks-order-check")
def xstocks_order_check(
    pair: PairOpt,
    side: Annotated[str, typer.Option(help="buy or sell")],
    size: Annotated[str, typer.Option(help="Base-asset quantity as a decimal")],
    price: Annotated[str, typer.Option(help="Limit price as a decimal")],
    expires_in: Annotated[int, typer.Option(min=30, max=2419200, help="GTD lifetime in seconds")] = 3600,
    authenticate: Annotated[
        bool, typer.Option(help="Ask Kraken to validate only; never place an order")
    ] = False,
) -> None:
    """Check public xStocks rules; optionally send an authenticated validation-only order."""
    from decimal import Decimal, InvalidOperation

    from kaupo.data.xstocks import XStocksClient
    from kaupo.domain import Side
    from kaupo.venues.xstocks_validation import validate_with_kraken, validation_order

    try:
        parsed_pair, parsed_side = Pair.parse(pair), Side(side)
        quantity, limit = Decimal(size), Decimal(price)
        if not quantity.is_finite() or quantity <= 0 or not limit.is_finite() or limit <= 0:
            raise ValueError("size and price must be positive finite decimals")
    except (ValueError, InvalidOperation) as exc:
        raise typer.BadParameter(f"Invalid order input: {exc}") from None
    settings = get_settings()
    if authenticate and (not settings.kraken_api_key or not settings.kraken_api_secret):
        raise typer.BadParameter("Kraken API credentials are missing")

    async def _run() -> dict[str, Any]:
        async with XStocksClient() as public:
            markets = await public.markets()
        market = next((market for market in markets if market.pair == parsed_pair), None)
        if market is None:
            raise ValueError(f"{parsed_pair} is not in Kraken's xStocks market list")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=expires_in)
        payload = validation_order(market, parsed_side, quantity, limit, expires_at, now=now)
        result: dict[str, Any] = {
            "checked_at": now.isoformat(),
            "pair": str(parsed_pair),
            "status": "public_rules_checked",
            "request": payload,
        }
        if authenticate:
            validated = await validate_with_kraken(
                settings.kraken_api_key,
                settings.kraken_api_secret,
                market,
                parsed_side,
                quantity,
                limit,
                expires_at,
                now=now,
            )
            result.update(validated)
            result["status"] = "exchange_validated"
        return result

    try:
        result = asyncio.run(_run())
    except ValueError as exc:
        err_console.print(f"Order check failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))


@ingest_app.command(name="xstocks-candles")
def ingest_xstocks_candles(
    pair: PairOpt,
    timeframe: TimeframeOpt = "1h",
    days: Annotated[int, typer.Option(min=1)] = 365,
    start: StartOpt = None,
    end: EndOpt = None,
    recent: Annotated[
        bool, typer.Option(help="Use recent OHLC instead of historical trades (720-bar limit)")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Import complete xStocks candles from public Kraken trades or recent OHLC."""
    _setup_logging(verbose)
    from kaupo.data.candles import get_candle_range
    from kaupo.data.xstocks import XStocksClient, ingest_xstock_candles
    from kaupo.db.session import get_sessionmaker

    start_dt, end_dt = _range(days, start, end)
    if start_dt >= end_dt:
        raise typer.BadParameter("--start must be before --end")
    p, tf = Pair.parse(pair), Timeframe.parse(timeframe)

    async def _run() -> tuple[int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        async with XStocksClient() as client:
            total = await ingest_xstock_candles(client, sm, p, tf, start_dt, end_dt, recent=recent)
        async with sm() as session:
            first, last, count = await get_candle_range(session, p, tf, exchange="kraken")
        return total, first, last, count

    total, first, last, count = asyncio.run(_run())
    source = "recent OHLC" if recent else "historical trades"
    console.print(f"[green]Ingested {total} xStocks candles[/green] for {p} {tf.value} from Kraken {source}")
    console.print("Only complete candles within the requested range are imported; empty periods stay empty.")
    if recent:
        console.print(
            "Kraken OHLC serves only the newest 720 bars; omit --recent for historical reconstruction."
        )
    if first is not None and last is not None:
        console.print(
            f"Database coverage: {count} candles, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC"
        )
    console.print(
        "Data import only. xStocks trading and market-calendar support remain outside this command."
    )


@ingest_app.command(name="xstocks-refresh")
def ingest_xstocks_refresh(
    config: Annotated[
        Path, typer.Option(help="JSON file with pairs, timeframes, and explicit trading schedules")
    ],
    verbose: VerboseOpt = False,
) -> None:
    """Refresh public xStocks candles and emit JSON coverage; gaps or errors exit 1."""
    _setup_logging(verbose)
    from kaupo.data.xstocks import XStocksClient
    from kaupo.data.xstocks_refresh import RefreshConfig, RefreshResult, refresh_xstocks
    from kaupo.db.session import get_sessionmaker

    try:
        manifest = RefreshConfig.model_validate_json(config.read_text())
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"Invalid refresh configuration: {exc}") from None
    now = datetime.now(UTC)

    async def _run() -> list[RefreshResult]:
        async with XStocksClient() as client:
            return await refresh_xstocks(manifest, get_sessionmaker(), client, now=now)

    results = asyncio.run(_run())
    ok = all(result.ok for result in results)
    typer.echo(
        json.dumps(
            {
                "checked_at": now.isoformat(),
                "source": "kraken_xstocks_recent_ohlc",
                "ok": ok,
                "results": [result.model_dump(mode="json") for result in results],
            }
        )
    )
    if not ok:
        raise typer.Exit(1)


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


@ingest_app.command(name="open-interest")
def ingest_open_interest(
    pair: PairOpt,
    days: Annotated[int, typer.Option(min=1)] = 30,
    start: StartOpt = None,
    end: EndOpt = None,
    exchange: ExchangeOpt = "binance",
    verbose: VerboseOpt = False,
) -> None:
    """Download open-interest history for the pair's base asset into Postgres.

    Open interest comes from the Binance USDT-margined perpetual of the base
    asset (BTC/EUR -> BTC perp), hourly snapshots. Binance serves only ~30
    days back, so the table is forward-collected: the daily refresh keeps it
    current and history accumulates. Kraken open interest is not supported.
    """
    _setup_logging(verbose)
    from kaupo.data.binance import BinanceClient
    from kaupo.data.ingest import backfill_open_interest
    from kaupo.data.open_interest import OI_EXCHANGE, get_oi_range
    from kaupo.db.session import get_sessionmaker

    if exchange != OI_EXCHANGE:
        raise typer.BadParameter(
            f"open-interest history is only served by {OI_EXCHANGE}; "
            f"--exchange {exchange} is not supported for open-interest"
        )

    start_dt, end_dt = _range(days, start, end)
    p = Pair.parse(pair)

    async def _run() -> tuple[int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        async with BinanceClient() as client:
            total = await backfill_open_interest(client, sm, p.base, start_dt, end_dt)
        async with sm() as session:
            first, last, count = await get_oi_range(session, OI_EXCHANGE, p.base)
        return total, first, last, count

    total, first, last, count = asyncio.run(_run())
    console.print(f"[green]Ingested {total} open-interest rows[/green] for {p.base} from {OI_EXCHANGE}")
    if first is None or last is None:
        return
    console.print(
        f"Database coverage: {count} open-interest rows, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC"
    )


@ingest_app.command(name="archive")
def ingest_archive(
    pair: PairOpt,
    source: Annotated[str, typer.Option(help="trades, metrics, or all")] = "all",
    days: Annotated[int, typer.Option(min=1)] = 30,
    start: StartOpt = None,
    end: EndOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Backfill from the Binance public archive (data.binance.vision).

    trades: spot aggTrades zipped CSVs, aggregated to daily order-flow rows
    (exchange "binance" in orderflow_daily; the book fields stay empty —
    the archive has no spot book history). metrics: USD-M perp daily metrics
    (open interest, long/short ratios) into futures_metrics_daily. Monthly
    files serve completed months, daily files the current one. Idempotent;
    safe for both the deep backfill and the daily top-up (--days 3).
    """
    _setup_logging(verbose)
    import httpx

    from kaupo.data import binance_archive
    from kaupo.data.futures_metrics import (
        METRICS_EXCHANGE,
        get_futures_metrics_range,
        upsert_futures_metrics_daily,
    )
    from kaupo.data.orderflow_daily import get_latest_orderflow_day, upsert_orderflow_daily
    from kaupo.db.session import get_sessionmaker
    from kaupo.domain import FuturesMetricsDaily, OrderflowDaily

    if source not in ("all", "trades", "metrics"):
        raise typer.BadParameter("--source must be trades, metrics, or all")

    start_dt, end_dt = _range(days, start, end)
    start_day, end_day = start_dt.date(), end_dt.date()
    p = Pair.parse(pair)
    spot_symbol = f"{p.base}{p.quote}"
    perp_symbol = f"{p.base}USDT"
    failures = 0
    http = httpx.Client(timeout=binance_archive.HTTP_TIMEOUT_SECONDS)

    def _merge(
        target: dict[date, binance_archive.TradeDayAgg], new: dict[date, binance_archive.TradeDayAgg]
    ) -> None:
        for day, agg in new.items():
            old = target.get(day)
            if old is None:
                target[day] = agg
                continue
            old.trade_count += agg.trade_count
            old.buy_count += agg.buy_count
            old.sell_count += agg.sell_count
            old.buy_volume += agg.buy_volume
            old.sell_volume += agg.sell_volume
            old.max_trade_size = max(old.max_trade_size, agg.max_trade_size)

    trade_days: dict[date, binance_archive.TradeDayAgg] = {}
    if source in ("all", "trades"):
        monthly = [
            k
            for k in binance_archive.list_archive_keys(
                http, binance_archive.spot_aggtrades_prefix(spot_symbol, "monthly")
            )
            if (d := binance_archive.key_date(k)) is not None and start_day <= d < end_day
        ]
        covered_months = set()
        for k in monthly:
            kd = binance_archive.key_date(k)
            assert kd is not None  # the filter above guarantees a date
            covered_months.add((kd.year, kd.month))
        daily = [
            k
            for k in binance_archive.list_archive_keys(
                http, binance_archive.spot_aggtrades_prefix(spot_symbol, "daily")
            )
            if (d := binance_archive.key_date(k)) is not None
            and start_day <= d < end_day
            and (d.year, d.month) not in covered_months
        ]
        for key in sorted(monthly + daily):
            url = f"{binance_archive.ARCHIVE_BASE}/{key}"
            try:
                file_days, malformed = binance_archive.parse_aggtrades(binance_archive.download(http, url))
            except Exception as exc:
                console.print(f"[red]FAILED {key}: {exc}[/red]")
                failures += 1
                continue
            trades = sum(a.trade_count for a in file_days.values())
            console.print(
                f"{key.rsplit('/', 1)[-1]}: {len(file_days)} days, {trades} trades, {malformed} malformed"
            )
            _merge(trade_days, file_days)

    metric_rows: list[FuturesMetricsDaily] = []
    if source in ("all", "metrics"):
        keys = [
            k
            for k in binance_archive.list_archive_keys(
                http, binance_archive.futures_metrics_prefix(perp_symbol)
            )
            if (d := binance_archive.key_date(k)) is not None and start_day <= d < end_day
        ]
        for key in sorted(keys):
            url = f"{binance_archive.ARCHIVE_BASE}/{key}"
            try:
                metric_rows.append(
                    binance_archive.parse_metrics_daily(
                        binance_archive.download(http, url), METRICS_EXCHANGE, p.base
                    )
                )
            except Exception as exc:
                console.print(f"[red]FAILED {key}: {exc}[/red]")
                failures += 1
                continue
        if keys:
            console.print(f"metrics: {len(metric_rows)} day rows for {p.base}")

    orderflow_rows = [
        OrderflowDaily(
            exchange="binance",  # the archive venue; Kraken rows stay the venue-true ones
            pair=str(p),
            day=day,
            trade_count=agg.trade_count,
            buy_count=agg.buy_count,
            sell_count=agg.sell_count,
            buy_volume=agg.buy_volume,
            sell_volume=agg.sell_volume,
            max_trade_size=agg.max_trade_size,
            book_snapshots=0,
            spread_mean_bps=None,
            spread_max_bps=None,
        )
        for day, agg in sorted(trade_days.items())
    ]

    async def _store() -> tuple[date | None, date | None, date | None, int]:
        sm = get_sessionmaker()
        async with sm() as session:
            if orderflow_rows:
                await upsert_orderflow_daily(session, orderflow_rows)
            if metric_rows:
                await upsert_futures_metrics_daily(session, metric_rows)
            await session.commit()
            latest_of = await get_latest_orderflow_day(session, "binance", str(p))
            fm_first, fm_last, fm_count = await get_futures_metrics_range(session, METRICS_EXCHANGE, p.base)
        return latest_of, fm_first, fm_last, fm_count

    latest_of, fm_first, fm_last, fm_count = asyncio.run(_store())
    if orderflow_rows:
        console.print(
            f"[green]Ingested {len(orderflow_rows)} order-flow day rows[/green] for {p} from binance"
            f" (latest in database: {latest_of})"
        )
    if metric_rows:
        console.print(
            f"[green]Ingested {len(metric_rows)} futures-metrics day rows[/green] for {p.base};"
            f" database coverage: {fm_count} rows, {fm_first} → {fm_last}"
        )
    if failures:
        raise typer.Exit(1)


TRADE_INGEST_MAX_DAYS = 31  # tick volume is large; one run stays bounded


@ingest_app.command(name="trades")
def ingest_trades(
    pair: PairOpt,
    days: Annotated[int, typer.Option(min=1, max=TRADE_INGEST_MAX_DAYS)] = 3,
    start: StartOpt = None,
    end: EndOpt = None,
    exchange: ExchangeOpt = "kraken",
    asset_class: Annotated[
        str, typer.Option(help="currency or tokenized_asset (Kraken xStocks)")
    ] = "currency",
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
    from kaupo.data.xstocks import XStocksClient, ingest_xstock_trades
    from kaupo.db.session import get_sessionmaker

    clients = {"kraken": KrakenClient, "binance": BinanceClient}
    if exchange not in clients:
        raise typer.BadParameter(f"--exchange must be one of: {', '.join(sorted(clients))}")

    if asset_class not in ("currency", "tokenized_asset"):
        raise typer.BadParameter("--asset-class must be currency or tokenized_asset")
    if asset_class == "tokenized_asset" and exchange != "kraken":
        raise typer.BadParameter("xStocks trades require --exchange kraken")

    start_dt, end_dt = _range(days, start, end)
    if start_dt >= end_dt:
        raise typer.BadParameter("--start must be before --end")
    if end_dt - start_dt > timedelta(days=TRADE_INGEST_MAX_DAYS):
        raise typer.BadParameter(f"trade ingest is capped at {TRADE_INGEST_MAX_DAYS} days per run")
    p = Pair.parse(pair)
    retention_days = get_settings().trades_retention_days

    async def _run() -> tuple[int, int, datetime | None, datetime | None, int]:
        sm = get_sessionmaker()
        if asset_class == "tokenized_asset":
            async with XStocksClient() as xstocks:
                total = await ingest_xstock_trades(xstocks, sm, p, start_dt, end_dt)
        else:
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


@ingest_app.command(name="orderflow-rollup")
def ingest_orderflow_rollup(
    pair: Annotated[
        list[str],
        typer.Option(help="e.g. BTC/EUR (repeatable; default: every pair with raw order-flow rows)"),
    ] = [],
    day: Annotated[str | None, typer.Option(help="UTC date, YYYY-MM-DD (default: yesterday)")] = None,
    exchange: Annotated[str, typer.Option(help="raw order-flow source exchange")] = "kraken",
    trading_schedule: Annotated[
        Path | None, typer.Option(help="Schedule JSON for one explicitly selected --pair")
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Roll up one UTC day of raw order flow into permanent daily aggregates.

    Computes one row per pair from the retention-capped trade ticks and book
    snapshots and upserts it; a rerun of the same day recomputes and
    overwrites the same rows. Run daily so the aggregates land before the
    raw rows age out of the 30-day retention window.
    """
    _setup_logging(verbose)
    from kaupo.data.orderflow_daily import (
        list_orderflow_source_pairs,
        rollup_orderflow_daily,
        upsert_orderflow_daily,
    )
    from kaupo.db.session import get_sessionmaker
    from kaupo.domain import OrderflowDaily

    try:
        target = date.fromisoformat(day) if day else datetime.now(UTC).date() - timedelta(days=1)
    except ValueError:
        raise typer.BadParameter(f"--day must be an ISO date (YYYY-MM-DD), got {day!r}") from None
    selected = [str(Pair.parse(p)) for p in pair]
    schedule = _load_trading_schedule(trading_schedule)
    if schedule is not None and len(selected) != 1:
        raise typer.BadParameter("--trading-schedule requires exactly one --pair")

    async def _run() -> list[OrderflowDaily]:
        sm = get_sessionmaker()
        async with sm() as session:
            pairs = selected or await list_orderflow_source_pairs(session, exchange)
            rows = [
                await rollup_orderflow_daily(session, exchange, p, target, trading_schedule=schedule)
                for p in pairs
            ]
            await upsert_orderflow_daily(session, rows)
            await session.commit()
        return rows

    rows = asyncio.run(_run())
    console.print(
        f"[green]Rolled up order flow[/green] for {len(rows)} pairs on {target:%Y-%m-%d} from {exchange}"
    )
    for row in rows:
        console.print(
            f"  {row.pair}: {row.trade_count} trades, {row.book_snapshots} book snapshots, "
            f"{row.session_state}"
        )


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
    instrument: Annotated[
        str, typer.Option(help="spot (long-only) or perp (shorts at 1x, funding charged)")
    ] = "spot",
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
    trading_schedule: Annotated[
        Path | None, typer.Option(help="JSON file containing a TradingSchedule")
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
    schedule = _load_trading_schedule(trading_schedule)
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
            if instrument not in ("spot", "perp"):
                err_console.print(f"[red]--instrument must be spot or perp, got {instrument!r}[/red]")
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
                instrument=instrument,
                risk=risk,
                persist=not no_persist,
                trading_schedule=schedule,
            )
        elif pairs is not None and pair is None:
            if not loaded.is_portfolio:
                err_console.print(
                    f"[red]Strategy {strategy!r} is not a portfolio strategy[/red] — use --pair"
                )
                raise typer.Exit(1)
            if instrument == "perp":
                err_console.print("[red]--instrument perp is single-pair only for now[/red]")
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
                trading_schedule=schedule,
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
            comparison = entry.get("comparison", {})
            backtest_sharpe = comparison.get("backtest_sharpe", backtest["sharpe"])
            shadow_sharpe = comparison.get("shadow_sharpe", shadow["sharpe"])
            cells = (
                f"sharpe {backtest_sharpe} ({backtest.get('num_fills', '—')} fills)",
                f"sharpe {shadow_sharpe} ({shadow['num_fills']} fills)",
                entry["verdict"],
            )
        table.add_row(entry["id"], entry["strategy_id"], entry["pair"], entry["timeframe"], *cells)
    console.print(table)
