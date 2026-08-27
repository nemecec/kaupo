"""The pair-quality universe: the EUR pairs the platform covers end to end.

One source of truth for the pair list. The candle/trade refresh on the host
(deploy/refresh-kraken.sh) keeps its own copy; the book collector uses this
one.
"""

from kaupo.domain import Pair

#: The 11 Kraken EUR pairs of the pair-quality universe.
KRAKEN_UNIVERSE: tuple[Pair, ...] = tuple(
    Pair.parse(p)
    for p in (
        "BTC/EUR",
        "ETH/EUR",
        "SOL/EUR",
        "XRP/EUR",
        "ADA/EUR",
        "LINK/EUR",
        "DOGE/EUR",
        "LTC/EUR",
        "AVAX/EUR",
        "DOT/EUR",
        "ATOM/EUR",
    )
)
