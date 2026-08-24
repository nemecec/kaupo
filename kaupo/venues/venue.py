"""Venue interface shared by paper and (later) real exchanges."""

from typing import Protocol

from kaupo.domain import Candle, Fill, Order, OrderId, Pair


class Venue(Protocol):
    def submit(self, order: Order) -> None:
        """Queue an order. It becomes eligible for execution on the *next* candle."""
        ...

    def on_candle(self, candle: Candle) -> list[Fill]:
        """Process a new candle; returns any fills it produced."""
        ...

    def drain_new_orders(self) -> list[Order]:
        """Orders created internally since the last drain (e.g. protective exits)."""
        ...

    def void_fill(self, fill: Fill) -> None:
        """Roll back a fill the ledger rejected (position tracking + order state)."""
        ...

    def get_order(self, order_id: OrderId) -> Order | None: ...

    def liquidate(self, pair: Pair, size: float, candle: Candle) -> Fill:
        """Force-close a position immediately at the given candle."""
        ...

    def cancel_all(self) -> list[Order]:
        """Cancel all open orders (returns them)."""
        ...
