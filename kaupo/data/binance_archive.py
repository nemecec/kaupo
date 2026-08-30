"""Binance public data archive (data.binance.vision): listing, download, parsers.

The archive publishes zipped CSVs the REST API never serves deep history
for: spot aggTrades (every public trade, years back) and USD-M futures
daily metrics (open interest and long/short ratios). Pure stdlib; no ccxt,
no database. The CLI turns the parsed rows into storage upserts.
"""

import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from kaupo.domain import FuturesMetricsDaily

log = logging.getLogger(__name__)

ARCHIVE_BASE = "https://data.binance.vision"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
HTTP_TIMEOUT_SECONDS = 120

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def spot_aggtrades_prefix(symbol: str, granularity: str) -> str:
    """Archive key prefix for spot aggTrades of a plain symbol (e.g. "BTCEUR")."""
    return f"data/spot/{granularity}/aggTrades/{symbol}/"


def futures_metrics_prefix(symbol: str) -> str:
    """Archive key prefix for USD-M futures daily metrics (e.g. "BTCUSDT")."""
    return f"data/futures/um/daily/metrics/{symbol}/"


def list_archive_keys(client: httpx.Client, prefix: str) -> list[str]:
    """All keys under an archive prefix (S3 listing), .CHECKSUM files excluded."""
    keys: list[str] = []
    marker = ""
    while True:
        params: dict[str, str | int] = {"prefix": prefix, "max-keys": 1000}
        if marker:
            params["marker"] = marker
        resp = client.get(LISTING_URL, params=params)
        resp.raise_for_status()
        # stdlib etree does not resolve external entities; the source is the
        # fixed S3 listing endpoint over TLS
        root = ET.fromstring(resp.content)  # noqa: S314
        page = [k.text or "" for k in root.iter(f"{_S3_NS}Key")]
        keys.extend(page)
        if root.find(f"{_S3_NS}IsTruncated").text != "true":  # type: ignore[union-attr]
            break
        marker = page[-1]
    return [k for k in keys if not k.endswith(".CHECKSUM")]


def download(client: httpx.Client, url: str) -> bytes:
    """One archive file as bytes."""
    log.info("Downloading %s", url)
    resp = client.get(url)
    resp.raise_for_status()
    return resp.content


_KEY_DATE = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


def key_date(key: str) -> date | None:
    """The date encoded in an archive file name (monthly keys give month precision)."""
    m = _KEY_DATE.search(key)
    if m is None:
        return None
    parts = [int(p) for p in m.group(1).split("-")]
    return date(parts[0], parts[1], parts[2] if len(parts) == 3 else 1)


@dataclass
class TradeDayAgg:
    """Mutable per-day accumulator for parsed aggTrades rows."""

    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    max_trade_size: float = 0.0


def parse_aggtrades(zip_bytes: bytes) -> tuple[dict[date, TradeDayAgg], int]:
    """Aggregate one aggTrades zip (monthly or daily) into per-UTC-day buckets.

    Returns (day buckets, malformed row count). Verified layout (2024 files,
    no header): agg_trade_id, price, quantity, first_trade_id, last_trade_id,
    timestamp_ms, is_buyer_maker, is_best_match. Newer files carry a header
    row; it is detected and skipped. The buyer-is-maker flag maps to taker
    side: True means the seller was the aggressor.
    """
    days: dict[date, TradeDayAgg] = {}
    malformed = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected exactly one CSV in the archive, got {names}")
        with zf.open(names[0]) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
            for row in reader:
                if not row or not row[0].strip().isdigit():
                    continue  # header row or blank line
                try:
                    qty = float(row[2])
                    ts_ms = int(row[5])
                    maker_is_buyer = row[6].strip().lower() == "true"
                except (IndexError, ValueError):
                    malformed += 1
                    continue
                if ts_ms <= 0:
                    malformed += 1
                    continue
                day = _day_of_ts(ts_ms)
                agg = days.setdefault(day, TradeDayAgg())
                agg.trade_count += 1
                if maker_is_buyer:  # buyer is the maker: the seller is the taker
                    agg.sell_count += 1
                    agg.sell_volume += qty
                else:
                    agg.buy_count += 1
                    agg.buy_volume += qty
                if qty > agg.max_trade_size:
                    agg.max_trade_size = qty
    return days, malformed


def parse_metrics_daily(zip_bytes: bytes, exchange: str, base_asset: str) -> FuturesMetricsDaily:
    """Aggregate one futures metrics daily zip (5-minute rows) into one day row.

    Open interest is the end-of-day snapshot (last complete row); the
    long/short ratios are day means over the rows where the column is
    present. Real-world gaps are tolerated: some files carry duplicated
    rows (deduped, the last wins), rows with zero OI and blank ratios, and
    rows with a blank taker ratio. A column with no usable value at all
    fails the file.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected exactly one CSV in the archive, got {names}")
        with zf.open(names[0]) as fh:
            raw = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))
    if not raw:
        raise ValueError("metrics archive has no data rows")
    # some files carry duplicated rows; the last occurrence wins
    by_time = {r["create_time"]: r for r in raw}
    rows = list(by_time.values())
    day = datetime.strptime(rows[0]["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).date()

    def values(column: str) -> list[float]:
        return [float(r[column]) for r in rows if (r[column] or "").strip() != ""]

    columns = (
        "count_toptrader_long_short_ratio",
        "sum_toptrader_long_short_ratio",
        "count_long_short_ratio",
        "sum_taker_long_short_vol_ratio",
    )
    # a listed perp never has exactly zero OI; '0E-8' rows are data gaps
    oi_rows = [
        r
        for r in rows
        if (r["sum_open_interest"] or "").strip() != ""
        and (r["sum_open_interest_value"] or "").strip() != ""
        and float(r["sum_open_interest"]) != 0.0
    ]
    means = [values(c) for c in columns]
    relevant = ("sum_open_interest", "sum_open_interest_value", *columns)
    malformed = sum(1 for r in rows if any((r[c] or "").strip() == "" for c in relevant))
    if malformed:
        log.warning("metrics day %s: %d row(s) with blank fields skipped", day, malformed)
    if not oi_rows:
        raise ValueError(f"metrics archive has no usable open-interest rows for {day}")
    if any(not v for v in means):
        raise ValueError(f"metrics archive has a fully blank ratio column for {day}")
    last = oi_rows[-1]

    return FuturesMetricsDaily(
        exchange=exchange,
        base_asset=base_asset,
        day=day,
        oi_base=float(last["sum_open_interest"]),
        oi_quote=float(last["sum_open_interest_value"]),
        count_toptrader_ls_ratio=_mean(means[0]),
        sum_toptrader_ls_ratio=_mean(means[1]),
        count_ls_ratio=_mean(means[2]),
        taker_ls_vol_ratio=_mean(means[3]),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _day_of_ts(ts: int) -> date:
    # Binance moved archive timestamps from milliseconds to microseconds
    # during 2025; 1e14 separates the eras (ms today ≈ 1.7e12, µs ≈ 1.7e15)
    seconds = ts / 1_000_000 if ts > 100_000_000_000_000 else ts / 1_000
    return datetime.fromtimestamp(seconds, tz=UTC).date()
