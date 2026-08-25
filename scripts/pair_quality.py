"""Pair-quality analysis of public exchange trades.

Standalone, read-only spike for issue #2. Downloads recent public trades with
ccxt (no API keys, public endpoints only) and computes per-pair quality
heuristics:

- round-size share and weekend volume share (documented retail fingerprints)
- first-digit Benford chi-square on trade sizes (Cong et al. 2023
  wash-trading heuristic)

Outputs raw metrics as JSON and a markdown report.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ccxt

DEFAULT_EXCHANGE = "kraken"
DEFAULT_PAIRS = [
    "BTC/EUR",
    "ETH/EUR",
    "SOL/EUR",
    "XRP/EUR",
    "ADA/EUR",
    "DOT/EUR",
    "LINK/EUR",
    "AVAX/EUR",
    "ATOM/EUR",
    "LTC/EUR",
    "DOGE/EUR",
]
DEFAULT_TARGET_TRADES = 10_000
DEFAULT_MIN_DAYS = 7.0
DEFAULT_MAX_REQUESTS = 60
FETCH_LIMIT = 1000  # max trades per fetch_trades call on Kraken and Binance
ROUND_REL_TOL = 1e-9
COVERAGE_TOLERANCE = 0.99  # grace for first-trade delay before flagging low confidence
BENFORD_DF = 8
SECONDS_PER_DAY = 86_400

BENFORD_PROBS: dict[int, float] = {d: math.log10(1 + 1 / d) for d in range(1, 10)}


# ---------------------------------------------------------------------------
# Data model


@dataclass
class Trade:
    """One public trade, normalised from a ccxt trade structure."""

    ts: datetime
    price: float
    amount: float  # base currency
    side: str | None
    trade_id: str | None

    @property
    def quote_volume(self) -> float:
        return self.price * self.amount


@dataclass
class PairMetrics:
    """Computed metrics for one symbol on one venue."""

    exchange: str
    symbol: str
    trade_count: int
    first_ts: str | None
    last_ts: str | None
    coverage_days: float
    median_size_quote: float | None
    trades_per_day: float | None
    daily_quote_volume: float | None
    round_share_count: float | None
    round_share_volume: float | None
    benford_chi2: float | None
    benford_observed: dict[str, float]
    weekend_volume_share: float | None
    buy_sell_ratio: float | None
    low_confidence: bool
    requests_used: int
    note: str | None = None


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_trade(raw: dict[str, Any]) -> Trade | None:
    """Normalise a ccxt trade dict; returns None for unusable entries."""
    ts_ms = _to_float(raw.get("timestamp"))
    price = _to_float(raw.get("price"))
    amount = _to_float(raw.get("amount"))
    if ts_ms is None or price is None or amount is None or amount <= 0:
        return None
    side = raw.get("side")
    trade_id = raw.get("id")
    return Trade(
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        price=price,
        amount=amount,
        side=str(side) if side in ("buy", "sell") else None,
        trade_id=str(trade_id) if trade_id is not None else None,
    )


# ---------------------------------------------------------------------------
# Pure metric functions


def first_digit(x: float) -> int:
    """First significant digit (1-9) of a positive finite number."""
    if x <= 0 or not math.isfinite(x):
        msg = f"first_digit needs a positive finite number, got {x}"
        raise ValueError(msg)
    while x >= 10:
        x /= 10
    while x < 1:
        x *= 10
    return int(x)


def benford_counts(amounts: list[float]) -> list[int]:
    """Observed first-digit counts (index 0 = digit 1) over trade sizes."""
    counts = [0] * 9
    for amount in amounts:
        if amount > 0 and math.isfinite(amount):
            counts[first_digit(amount) - 1] += 1
    return counts


def benford_chi2(counts: list[int]) -> float:
    """Chi-square statistic vs Benford expectation (8 degrees of freedom)."""
    n = sum(counts)
    if n == 0:
        return float("nan")
    return sum(
        (observed - n * p) ** 2 / (n * p) for observed, p in zip(counts, BENFORD_PROBS.values(), strict=True)
    )


def benford_distribution(counts: list[int]) -> dict[int, float]:
    """Observed share per first digit; zeros when the sample is empty."""
    n = sum(counts)
    return {d: (counts[d - 1] / n if n else 0.0) for d in range(1, 10)}


def round_to_sig(x: float, sig: int) -> float:
    """Round to ``sig`` significant digits."""
    if x == 0:
        return 0.0
    return round(x, sig - 1 - math.floor(math.log10(abs(x))))


def is_round_size(amount: float) -> bool:
    """True when the base amount equals its 1- or 2-significant-digit rounding."""
    if amount <= 0 or not math.isfinite(amount):
        return False
    return any(
        math.isclose(amount, round_to_sig(amount, sig), rel_tol=ROUND_REL_TOL, abs_tol=0.0) for sig in (1, 2)
    )


def round_size_shares(trades: list[Trade]) -> tuple[float, float] | None:
    """Round-size share (by count, by quote volume); None on empty input."""
    if not trades:
        return None
    total_volume = sum(t.quote_volume for t in trades)
    round_count = sum(1 for t in trades if is_round_size(t.amount))
    round_volume = sum(t.quote_volume for t in trades if is_round_size(t.amount))
    by_volume = round_volume / total_volume if total_volume > 0 else 0.0
    return round_count / len(trades), by_volume


def weekend_volume_share(trades: list[Trade]) -> float | None:
    """Share of quote volume traded on UTC Saturday and Sunday."""
    total_volume = sum(t.quote_volume for t in trades)
    if total_volume <= 0:
        return None
    weekend = sum(t.quote_volume for t in trades if t.ts.weekday() >= 5)
    return weekend / total_volume


def buy_sell_count_ratio(trades: list[Trade]) -> float | None:
    """Buy/sell ratio by count; None when side data is absent or degenerate."""
    buys = sum(1 for t in trades if t.side == "buy")
    sells = sum(1 for t in trades if t.side == "sell")
    if buys == 0 or sells == 0:
        return None
    return buys / sells


def is_low_confidence(coverage_days: float, min_days: float) -> bool:
    """True when coverage falls short of the target window.

    A 1% tolerance absorbs the gap between the requested window start and the
    first actual trade, so a full-window pair is not flagged by rounding.
    """
    return coverage_days < min_days * COVERAGE_TOLERANCE


def compute_metrics(
    exchange_id: str,
    symbol: str,
    trades: list[Trade],
    min_days: float,
    requests_used: int,
    note: str | None = None,
) -> PairMetrics:
    """Compute all per-pair metrics from a fetched trade list."""
    trades = sorted(trades, key=lambda t: t.ts)
    first_ts = trades[0].ts if trades else None
    last_ts = trades[-1].ts if trades else None
    coverage_days = (last_ts - first_ts).total_seconds() / SECONDS_PER_DAY if first_ts and last_ts else 0.0
    total_quote_volume = sum(t.quote_volume for t in trades)
    counts = benford_counts([t.amount for t in trades])
    chi2 = benford_chi2(counts)
    round_shares = round_size_shares(trades)
    return PairMetrics(
        exchange=exchange_id,
        symbol=symbol,
        trade_count=len(trades),
        first_ts=first_ts.isoformat(timespec="seconds") if first_ts else None,
        last_ts=last_ts.isoformat(timespec="seconds") if last_ts else None,
        coverage_days=coverage_days,
        median_size_quote=statistics.median([t.quote_volume for t in trades]) if trades else None,
        trades_per_day=len(trades) / coverage_days if coverage_days > 0 else None,
        daily_quote_volume=total_quote_volume / coverage_days if coverage_days > 0 else None,
        round_share_count=round_shares[0] if round_shares else None,
        round_share_volume=round_shares[1] if round_shares else None,
        benford_chi2=chi2 if math.isfinite(chi2) else None,
        benford_observed={str(d): share for d, share in benford_distribution(counts).items()},
        weekend_volume_share=weekend_volume_share(trades),
        buy_sell_ratio=buy_sell_count_ratio(trades),
        low_confidence=is_low_confidence(coverage_days, min_days),
        requests_used=requests_used,
        note=note,
    )


# ---------------------------------------------------------------------------
# Fetching (read-only, public endpoints)


def make_exchange(exchange_id: str) -> Any:
    """Create a ccxt exchange instance for public data only."""
    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        print(f"error: ccxt has no exchange '{exchange_id}'", file=sys.stderr)
        raise SystemExit(2)
    exchange = exchange_class({"enableRateLimit": True})
    if exchange_id == "binance":
        # publicGetAggTrades supports startTime-based paging; publicGetTrades does not
        exchange.options["fetchTradesMethod"] = "publicGetAggTrades"
    return exchange


def resolve_symbols(exchange: Any, requested: list[str]) -> tuple[list[str], list[str]]:
    """Split requested symbols into (found, skipped) using load_markets()."""
    exchange.load_markets()
    markets = exchange.markets
    found = [s for s in requested if s in markets]
    skipped = [s for s in requested if s not in markets]
    for symbol in skipped:
        print(f"warning: symbol {symbol} not on {exchange.id}; skipping", file=sys.stderr)
    return found, skipped


def _fetch_page(
    exchange: Any, symbol: str, since_ms: int | None, params: dict[str, str]
) -> list[dict[str, Any]]:
    """One fetch_trades call with limited retries on network errors."""
    for attempt in range(3):
        try:
            raw: list[dict[str, Any]] = exchange.fetch_trades(
                symbol, since=since_ms, limit=FETCH_LIMIT, params=params
            )
            return raw
        except ccxt.NetworkError as exc:
            wait = 2.0**attempt
            print(f"[{symbol}] network error ({exc}); retry in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    print(f"[{symbol}] giving up after 3 network errors", file=sys.stderr)
    return []


def _kraken_cursor(raw: list[dict[str, Any]], last_ts: datetime) -> str:
    """Next Kraken ``since`` cursor: a nanosecond timestamp.

    ccxt stuffs the response-level ``last`` cursor into the info of the last
    trade. Fall back to the last trade timestamp (ms -> ns) when it is absent;
    the slight overlap is removed by id-based dedupe.
    """
    if raw:
        info = raw[-1].get("info")
        if isinstance(info, list) and len(info) > 7:
            return str(info[7])
    return str(int(last_ts.timestamp() * 1000) * 1_000_000)


def fetch_recent_trades(
    exchange: Any,
    symbol: str,
    target_trades: int,
    min_days: float,
    max_requests: int,
) -> tuple[list[Trade], int]:
    """Page forward through public trades. Returns (trades, requests used).

    The window starts at ``now - min_days`` and pages forward toward now. It
    stops when both targets (trade count and day coverage) are met, when the
    request cap is hit, or when the live edge is reached.

    Pagination differs per venue: Kraken needs its nanosecond ``since`` cursor
    (its ms-timestamp handling is broken), other venues get a ms ``since``.
    Binance serves aggTrades in one-hour windows, so empty pages there mean an
    empty hour, not the live edge; the window is skipped forward instead.
    """
    kraken_cursor_mode = exchange.id == "kraken"
    trades: list[Trade] = []
    seen: set[str] = set()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = now_ms - int(min_days * SECONDS_PER_DAY * 1000)
    since_ms: int | None = None if kraken_cursor_mode else start_ms
    cursor: str | None = str(start_ms * 1_000_000) if kraken_cursor_mode else None
    requests = 0
    while requests < max_requests:
        params = {"since": cursor} if kraken_cursor_mode and cursor else {}
        try:
            raw = _fetch_page(exchange, symbol, None if kraken_cursor_mode else since_ms, params)
        except ccxt.ExchangeError as exc:
            print(f"[{symbol}] exchange error ({exc}); stopping pair", file=sys.stderr)
            break
        requests += 1
        parsed = [t for t in (parse_trade(r) for r in raw) if t is not None]
        new = [t for t in parsed if t.trade_id is None or t.trade_id not in seen]
        if not new:
            if kraken_cursor_mode or (since_ms is not None and since_ms >= now_ms):
                print(f"[{symbol}] caught up to the live edge; stopping", file=sys.stderr)
                break
            # Binance aggTrades window with no trades: skip the hour forward.
            since_ms = (since_ms or start_ms) + 3_600_000
            continue
        for t in new:
            if t.trade_id is not None:
                seen.add(t.trade_id)
        trades.extend(new)
        first_ts = min(t.ts for t in trades)
        last_ts = max(t.ts for t in trades)
        if kraken_cursor_mode:
            cursor = _kraken_cursor(raw, last_ts)
        else:
            since_ms = int(last_ts.timestamp() * 1000) + 1
        coverage_days = (last_ts - first_ts).total_seconds() / SECONDS_PER_DAY
        print(
            f"[{exchange.id} {symbol}] req {requests}/{max_requests}: "
            f"{len(trades)} trades, {coverage_days:.2f} days",
            file=sys.stderr,
        )
        at_live_edge = int(last_ts.timestamp() * 1000) >= now_ms - 15 * 60 * 1000
        if len(new) < FETCH_LIMIT // 20 and at_live_edge:
            print(f"[{symbol}] caught up to the live edge; stopping", file=sys.stderr)
            break
        if len(trades) >= target_trades and coverage_days >= min_days:
            break
    return trades, requests


# ---------------------------------------------------------------------------
# Report


def _fmt_num(value: float | None, fmt: str = ",.0f") -> str:
    return format(value, fmt) if value is not None else "n/a"


def _fmt_pct(value: float | None) -> str:
    return f"{100 * value:.1f}" if value is not None else "n/a"


def suspicion_ranking(metrics: list[PairMetrics]) -> list[tuple[str, float]]:
    """Rank pairs by mean rank of the three fingerprint metrics.

    Lower mean rank means cleaner (more institutional-looking) flow. Pairs
    with missing data rank last.
    """

    def ranks(values: list[float | None]) -> list[float | None]:
        present = sorted(v for v in values if v is not None)
        return [(present.index(v) / max(len(present) - 1, 1) if v is not None else None) for v in values]

    chi2_ranks = ranks([m.benford_chi2 for m in metrics])
    round_ranks = ranks([m.round_share_volume for m in metrics])
    # weekend share is a time-based metric: skip it for low-confidence windows
    weekend_ranks = ranks([None if m.low_confidence else m.weekend_volume_share for m in metrics])
    scored: list[tuple[str, float]] = []
    for m, rc, rr, rw in zip(metrics, chi2_ranks, round_ranks, weekend_ranks, strict=True):
        parts = [r for r in (rc, rr, rw) if r is not None]
        label = f"{m.symbol} ({m.exchange})"
        scored.append((label, sum(parts) / len(parts) if parts else float("inf")))
    return sorted(scored, key=lambda item: item[1])


def build_markdown(
    generated: datetime,
    args: argparse.Namespace,
    metrics: list[PairMetrics],
    skipped: list[str],
) -> str:
    """Render the markdown report."""
    lines: list[str] = []
    lines.append(f"# Pair quality report — {generated:%Y-%m}")
    lines.append("")
    lines.append(f"Generated {generated:%Y-%m-%d %H:%M} UTC by `scripts/pair_quality.py` (issue #2).")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(f"The script downloads recent public trades from {args.exchange} with the ccxt library.")
    lines.append("No API keys are used. Only public endpoints are called. No orders are placed.")
    lines.append(
        f"For each pair the script pages through the trade history until it has"
        f" {args.target_trades} trades and {args.min_days:g} days of coverage,"
        f" or until it hits the cap of {args.max_requests_per_pair} requests per pair."
    )
    lines.append(
        "Pairs with less coverage than the minimum days are marked low confidence."
        " Time-based metrics for these pairs rest on a short window."
    )
    lines.append("")
    lines.append("The metrics follow published heuristics:")
    lines.append("")
    lines.append(
        "- Benford first-digit test on trade sizes. Cong et al. (2023) show that wash trading"
        " distorts the first-digit distribution. The chi-square statistic (8 degrees of freedom)"
        " measures the deviation from the Benford law. Large values are a warning sign."
    )
    lines.append(
        "- Round-size share. A trade counts as round when its base size equals its own value"
        " rounded to 1 or 2 significant digits. Round sizes are a documented retail fingerprint."
    )
    lines.append(
        "- Weekend volume share (UTC Saturday and Sunday). Retail flow is active on weekends."
        " Institutional flow concentrates on weekdays."
    )
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append(
        "| Pair | Venue | Coverage (days) | Trades | Median size (EUR) | Daily volume (EUR) |"
        " Round share % (cnt/vol) | Benford χ² | Weekend vol % | Buy/sell | Flags |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    ordered = sorted(metrics, key=lambda m: (m.daily_quote_volume is None, -(m.daily_quote_volume or 0)))
    for m in ordered:
        flags: list[str] = []
        if m.low_confidence:
            flags.append("low confidence")
        if m.note:
            flags.append(m.note)
        lines.append(
            f"| {m.symbol} | {m.exchange} | {m.coverage_days:.2f} | {m.trade_count} |"
            f" {_fmt_num(m.median_size_quote, ',.4g')} | {_fmt_num(m.daily_quote_volume)} |"
            f" {_fmt_pct(m.round_share_count)} / {_fmt_pct(m.round_share_volume)} |"
            f" {_fmt_num(m.benford_chi2, ',.1f')} | {_fmt_pct(m.weekend_volume_share)} |"
            f" {_fmt_num(m.buy_sell_ratio, '.2f')} | {', '.join(flags) or '—'} |"
        )
    lines.append("")
    lines.append("The table is sorted by estimated daily quote volume.")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    for m in ordered:
        lines.append(
            f"- {m.symbol} ({m.exchange}): {m.first_ts} to {m.last_ts},"
            f" {m.trade_count} trades, {m.requests_used} requests."
            + (" LOW CONFIDENCE: coverage under the minimum days." if m.low_confidence else "")
        )
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    lines.append(
        "Ranking from most institutionally clean to most suspicious."
        " The score is the mean normalised rank of three metrics:"
        " Benford chi-square, round-size share by volume, and weekend volume share."
        " Low-confidence pairs do not score the weekend metric."
        " Lower is cleaner."
    )
    lines.append("")
    for i, (label, score) in enumerate(suspicion_ranking(metrics), start=1):
        lines.append(f"{i}. {label} — score {score:.2f}")
    lines.append("")
    lines.append("Caveats:")
    lines.append("")
    lines.append("- These are heuristics on one venue and one recent window. They are not proof.")
    lines.append("- A high Benford chi-square has many possible causes. Wash trading is one cause.")
    lines.append("- Round sizes and weekend activity point at retail flow, not at manipulation.")
    lines.append("- Coarse lot-size quantisation inflates the round-size share. Compare venues with care.")
    lines.append("- Low-liquidity pairs give unstable statistics. Treat their ranks with care.")
    lines.append("- The Binance row uses aggregated trades (aggTrades). It is a comparison, not a peer.")
    lines.append("")
    lines.append("## Skipped symbols")
    lines.append("")
    if skipped:
        for symbol in skipped:
            lines.append(f"- {symbol}: not found on {args.exchange} via load_markets().")
    else:
        lines.append("None. All requested symbols resolved.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only pair-quality analysis of public exchange trades (issue #2 spike)."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS)
    parser.add_argument("--target-trades", type=int, default=DEFAULT_TARGET_TRADES)
    parser.add_argument("--min-days", type=float, default=DEFAULT_MIN_DAYS)
    parser.add_argument("--max-requests-per-pair", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(f"docs/pair-quality-{datetime.now(UTC):%Y-%m}.md"),
        help="markdown report path; the JSON metrics file uses the same stem",
    )
    parser.add_argument(
        "--binance-compare",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="add a one-venue Binance comparison row for BTC/EUR",
    )
    return parser.parse_args(argv)


def _analyze(
    exchange_id: str,
    symbols: list[str],
    args: argparse.Namespace,
    note: str | None = None,
) -> tuple[list[PairMetrics], list[str]]:
    exchange = make_exchange(exchange_id)
    found, skipped = resolve_symbols(exchange, symbols)
    results: list[PairMetrics] = []
    for symbol in found:
        trades, requests = fetch_recent_trades(
            exchange,
            symbol,
            target_trades=args.target_trades,
            min_days=args.min_days,
            max_requests=args.max_requests_per_pair,
        )
        results.append(compute_metrics(exchange_id, symbol, trades, args.min_days, requests, note=note))
    return results, skipped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    metrics, skipped = _analyze(args.exchange, args.pairs, args)
    if args.binance_compare:
        binance_metrics, binance_skipped = _analyze(
            "binance",
            ["BTC/EUR"],
            args,
            note="aggregated trades",
        )
        metrics.extend(binance_metrics)
        skipped.extend(f"binance:{s}" for s in binance_skipped)

    generated = datetime.now(UTC)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out.with_suffix(".json")
    payload = {
        "generated_at": generated.isoformat(timespec="seconds"),
        "params": {
            "exchange": args.exchange,
            "pairs": args.pairs,
            "target_trades": args.target_trades,
            "min_days": args.min_days,
            "max_requests_per_pair": args.max_requests_per_pair,
            "binance_compare": args.binance_compare,
        },
        "metrics": [asdict(m) for m in metrics],
        "skipped": skipped,
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    args.out.write_text(build_markdown(generated, args, metrics, skipped))
    print(f"wrote {json_path} and {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
