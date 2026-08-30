"""binance_archive: aggTrades/metrics parsers, key dates, S3 listing."""

import io
import zipfile
from datetime import date

from kaupo.data.binance_archive import key_date, list_archive_keys, parse_aggtrades, parse_metrics_daily


def make_zip(csv_text: str, name: str = "data.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, csv_text)
    return buf.getvalue()


def ms(day: str, hhmmss: str = "00:00:00") -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(f"{day}T{hhmmss}+00:00").timestamp() * 1000)


# --- parse_aggtrades ---------------------------------------------------------

AGG_NO_HEADER = "\n".join(
    [
        # id, price, qty, first, last, ts_ms, is_buyer_maker, is_best_match
        f"101,100.0,1.5,1,2,{ms('2024-01-15', '00:00:01')},False,True",  # buy
        f"102,100.5,2.5,3,3,{ms('2024-01-15', '01:00:00')},True,True",  # sell
        f"103,99.5,4.0,4,5,{ms('2024-01-15', '23:59:59')},False,True",  # buy, max size
        f"104,99.0,1.0,6,6,{ms('2024-01-16', '00:00:00')},True,True",  # sell, next day
        "105,broken,row,6,6,badts,False,True",  # numeric id but unparsable fields
        "",
    ]
)


def test_parse_aggtrades_buckets_days_and_sides() -> None:
    days, malformed = parse_aggtrades(make_zip(AGG_NO_HEADER))

    assert malformed == 1  # the broken row; the blank line is skipped silently
    assert set(days) == {date(2024, 1, 15), date(2024, 1, 16)}
    first = days[date(2024, 1, 15)]
    assert first.trade_count == 3
    assert (first.buy_count, first.sell_count) == (2, 1)  # True = buyer is maker = sell
    assert first.buy_volume == 5.5
    assert first.sell_volume == 2.5
    assert first.max_trade_size == 4.0
    second = days[date(2024, 1, 16)]
    assert (second.trade_count, second.sell_count) == (1, 1)


def test_parse_aggtrades_skips_a_header_row() -> None:
    with_header = (
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,timestamp,is_buyer_maker,is_best_match\n"
        + AGG_NO_HEADER
    )
    days, malformed = parse_aggtrades(make_zip(with_header))
    assert malformed == 1
    assert days[date(2024, 1, 15)].trade_count == 3


def test_parse_aggtrades_picks_the_root_csv_among_fsx_duplicates() -> None:
    # real shape of the 2021-12 monthly zips: the data CSV plus a duplicate
    # under an fsx-data/... path
    buf = io.BytesIO()
    row = f"101,100.0,1.5,1,2,{ms('2021-12-15', '00:00:01')},False,True"
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fsx-data/collector_data/data/spot/monthly/aggTrades/BTCEUR/dup.csv", "9,9,9,9,9,9,9,9")
        zf.writestr("BTCEUR-aggTrades-2021-12.csv", row)
    days, malformed = parse_aggtrades(buf.getvalue())
    assert list(days) == [date(2021, 12, 15)]
    assert malformed == 0


def test_parse_aggtrades_rejects_ambiguous_multi_csv() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nested/a.csv", "x")
        zf.writestr("nested/b.csv", "y")
    try:
        parse_aggtrades(buf.getvalue())
    except ValueError as exc:
        assert "cannot pick a CSV" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_aggtrades_microsecond_timestamps() -> None:
    # Binance moved archive timestamps from ms to µs during 2025
    us = ms("2026-08-01", "00:00:01") * 1000
    csv_text = f"101,100.0,1.5,1,2,{us},False,True\n{us + 1},100.5,2.5,3,3,{us + 1},True,True"
    days, malformed = parse_aggtrades(make_zip(csv_text))

    assert malformed == 0
    assert list(days) == [date(2026, 8, 1)]
    agg = days[date(2026, 8, 1)]
    assert (agg.trade_count, agg.buy_count, agg.sell_count) == (2, 1, 1)


# --- parse_metrics_daily -----------------------------------------------------

METRICS_CSV = "\n".join(
    [
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio",
        "2024-01-15 00:00:00,BTCUSDT,100.0,5000000.0,2.0,1.0,3.0,1.0",
        "2024-01-15 00:05:00,BTCUSDT,200.0,9000000.0,4.0,2.0,5.0,3.0",
        "2024-01-15 23:55:00,BTCUSDT,300.0,12000000.0,6.0,3.0,7.0,5.0",
    ]
)


def test_parse_metrics_daily_eod_oi_and_mean_ratios() -> None:
    row = parse_metrics_daily(make_zip(METRICS_CSV), "binance", "BTC")

    assert row.exchange == "binance"
    assert row.base_asset == "BTC"
    assert row.day == date(2024, 1, 15)
    assert row.oi_base == 300.0  # last row, not the mean
    assert row.oi_quote == 12000000.0
    assert row.count_toptrader_ls_ratio == 4.0  # mean of 2, 4, 6
    assert row.sum_toptrader_ls_ratio == 2.0
    assert row.count_ls_ratio == 5.0
    assert row.taker_ls_vol_ratio == 3.0


def test_parse_metrics_daily_tolerates_blanks_and_duplicates() -> None:
    # row 2 has a blank taker ratio, row 3 has zero OI with blank ratios
    # (real gaps in the Binance files), row 4 duplicates row 1
    csv_text = "\n".join(
        [
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio",
            "2024-01-15 00:00:00,BTCUSDT,100.0,5000000.0,2.0,1.0,3.0,1.0",
            "2024-01-15 00:05:00,BTCUSDT,200.0,9000000.0,4.0,2.0,5.0,",
            "2024-01-15 00:10:00,BTCUSDT,0E-8,0E-8,,,,3.0",
            "2024-01-15 00:00:00,BTCUSDT,100.0,5000000.0,2.0,1.0,3.0,1.0",
        ]
    )
    row = parse_metrics_daily(make_zip(csv_text), "binance", "BTC")

    assert row.oi_base == 200.0  # the last row with usable OI (the zero-OI row is skipped)
    assert row.oi_quote == 9000000.0
    # the duplicate is deduped (the last wins); blank fields skip their column only
    assert row.count_toptrader_ls_ratio == 3.0  # mean of 2, 4 (zero-OI row blank)
    assert row.taker_ls_vol_ratio == 2.0  # mean of 1, 3 (blank and zero-OI rows skip)


def test_parse_metrics_daily_fully_blank_ratio_column_keeps_oi() -> None:
    # the 2022 gap era: ratio columns empty for the whole day, OI valid
    csv_text = "\n".join(
        [
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio",
            '2022-01-01 00:00:00,BTCUSDT,74803.41,3456683790.32,"","","",""',
            '2022-01-01 00:05:00,BTCUSDT,74861.62,3468073411.65,"","","",""',
        ]
    )
    row = parse_metrics_daily(make_zip(csv_text), "binance", "BTC")

    assert row.oi_base == 74861.62  # end-of-day OI, kept
    assert row.count_toptrader_ls_ratio is None
    assert row.sum_toptrader_ls_ratio is None
    assert row.count_ls_ratio is None
    assert row.taker_ls_vol_ratio is None


def test_parse_metrics_daily_fully_blank_oi_fails() -> None:
    csv_text = "\n".join(
        [
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio",
            "2024-01-15 00:00:00,BTCUSDT,0E-8,0E-8,,1.0,3.0,1.0",
            "2024-01-15 00:05:00,BTCUSDT,0E-8,0E-8,,2.0,5.0,3.0",
        ]
    )
    try:
        parse_metrics_daily(make_zip(csv_text), "binance", "BTC")
    except ValueError as exc:
        assert "open-interest" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# --- key_date ----------------------------------------------------------------


def test_key_date_monthly_and_daily() -> None:
    assert key_date("data/spot/monthly/aggTrades/BTCEUR/BTCEUR-aggTrades-2024-01.zip") == date(2024, 1, 1)
    assert key_date("data/spot/daily/aggTrades/BTCEUR/BTCEUR-aggTrades-2024-01-15.zip") == date(2024, 1, 15)
    assert key_date("data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-15.zip") == date(
        2024, 1, 15
    )
    assert key_date("data/spot/monthly/aggTrades/BTCEUR/BTCEUR-aggTrades-2024-01.zip.CHECKSUM") is None


# --- list_archive_keys --------------------------------------------------------

NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def listing_xml(keys: list[str], truncated: bool) -> bytes:
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    return (
        f'<ListBucketResult xmlns="{NS}"><IsTruncated>{"true" if truncated else "false"}</IsTruncated>'
        f"{body}</ListBucketResult>"
    ).encode()


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.content = payload

    def raise_for_status(self) -> None:
        pass


class FakeClient:
    """Stands in for httpx.Client: returns listing pages in order."""

    def __init__(self, pages: list[bytes]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None) -> FakeResponse:
        self.calls.append({"url": url, "params": params or {}})
        return FakeResponse(self.pages[len(self.calls) - 1])


def test_list_archive_keys_pages_and_drops_checksums() -> None:
    client = FakeClient(
        [
            listing_xml(["data/x/a.zip", "data/x/a.zip.CHECKSUM", "data/x/b.zip"], truncated=True),
            listing_xml(["data/x/c.zip"], truncated=False),
        ]
    )

    keys = list_archive_keys(client, "data/x/")  # type: ignore[arg-type]

    assert keys == ["data/x/a.zip", "data/x/b.zip", "data/x/c.zip"]
    assert len(client.calls) == 2
    assert client.calls[1]["params"]["marker"] == "data/x/b.zip"  # pages forward from the last key
