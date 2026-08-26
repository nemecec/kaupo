"""Portfolio accounting: cash, positions, append-only entry log.

In-memory source of truth during a run; the engine persists entries and
equity snapshots to Postgres. Money math uses Decimal; market prices come
in as floats and are converted at the boundary.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kaupo.domain import Fill, Pair, Position, Side

QUOTE_PRECISION = Decimal("0.00000001")


@dataclass(frozen=True)
class LedgerEntry:
    ts: datetime
    asset: str
    amount: Decimal
    balance_after: Decimal
    reason: str
    ref_id: str | None = None
    id: str = ""  # filled by persistence layer


class InsufficientFunds(Exception):
    pass


class InsufficientPosition(Exception):
    pass


def _dec(value: float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class Ledger:
    def __init__(
        self,
        quote_asset: str,
        starting_cash: float | Decimal,
        ts: datetime,
        *,
        positions: Mapping[Pair, Position] | None = None,
    ) -> None:
        self._quote = quote_asset
        self._cash = _dec(starting_cash)
        self._positions: dict[Pair, Position] = {
            pair: Position(pair=pos.pair, size=pos.size, avg_entry=pos.avg_entry)
            for pair, pos in (positions or {}).items()
            if pos.size != 0
        }
        self.entries: list[LedgerEntry] = []  # unpersisted entries
        self.realized_pnl: Decimal = Decimal(0)
        if positions is None:
            self._record(ts, quote_asset, self._cash, "deposit", None)
        else:
            # a resumed run: the opening balance carries in from the
            # predecessor run's chain, it is not a fresh deposit
            self._record(ts, quote_asset, self._cash, "carry-in", None)
            for pair, pos in self._positions.items():
                self._record(ts, pair.base, _dec(pos.size), "carry-in", None)

    def _record(self, ts: datetime, asset: str, amount: Decimal, reason: str, ref_id: str | None) -> None:
        balance = self._cash if asset == self._quote else _dec(self.position_for(asset).size)
        self.entries.append(
            LedgerEntry(
                ts=ts, asset=asset, amount=amount, balance_after=balance, reason=reason, ref_id=ref_id
            )
        )

    @property
    def quote_asset(self) -> str:
        return self._quote

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def open_positions(self) -> dict[Pair, Position]:
        """All open positions (copies), keyed by pair — the state a resume carries."""
        return {
            pair: Position(pair=pos.pair, size=pos.size, avg_entry=pos.avg_entry)
            for pair, pos in self._positions.items()
            if pos.size != 0
        }

    def position(self, pair: Pair) -> Position:
        # a copy: the SDK contract promises strategies a read-only view
        pos = self._positions.get(pair, Position(pair=pair))
        return Position(pair=pos.pair, size=pos.size, avg_entry=pos.avg_entry)

    def position_for(self, base_asset: str) -> Position:
        for pair, pos in self._positions.items():
            if pair.base == base_asset:
                return pos
        return Position(pair=Pair(base=base_asset, quote=self._quote))

    def apply_fill(self, fill: Fill) -> Decimal:
        """Apply a fill; returns realized PnL (nonzero only on closing sells)."""
        quote_amount = _dec(fill.price) * _dec(fill.size)
        fee = _dec(fill.fee)
        pos = self._positions.get(fill.pair, Position(pair=fill.pair))
        realized = Decimal(0)

        if fill.side is Side.BUY:
            cost = quote_amount + fee
            if cost > self._cash:
                raise InsufficientFunds(f"need {cost} {self._quote}, have {self._cash}")
            self._cash -= cost
            new_size = _dec(pos.size) + _dec(fill.size)
            # cost basis includes the entry fee so realized PnL covers both fees
            pos.avg_entry = (
                float((_dec(pos.size) * _dec(pos.avg_entry) + quote_amount + fee) / new_size)
                if new_size > 0
                else 0.0
            )
            pos.size = float(new_size)
            self._positions[fill.pair] = pos
            self._record(fill.ts, self._quote, -cost, "trade", fill.order_id)
            self._record(fill.ts, fill.pair.base, _dec(fill.size), "trade", fill.order_id)
        else:
            if _dec(fill.size) > _dec(pos.size):
                raise InsufficientPosition(f"sell {fill.size} of {fill.pair}, have {pos.size}")
            proceeds = quote_amount - fee
            self._cash += proceeds
            realized = _dec(fill.size) * (_dec(fill.price) - _dec(pos.avg_entry)) - fee
            pos.size = float(_dec(pos.size) - _dec(fill.size))
            if pos.size == 0:
                pos.avg_entry = 0.0
            self._positions[fill.pair] = pos
            self.realized_pnl += realized
            self._record(fill.ts, self._quote, proceeds, "trade", fill.order_id)
            self._record(fill.ts, fill.pair.base, -_dec(fill.size), "trade", fill.order_id)

        return realized

    def equity(self, prices: dict[Pair, float]) -> Decimal:
        value = self._cash
        for pair, pos in self._positions.items():
            if pos.size and pair in prices:
                value += _dec(pos.size) * _dec(prices[pair])
        return value

    def drain_entries(self) -> list[LedgerEntry]:
        entries, self.entries = self.entries, []
        return entries
