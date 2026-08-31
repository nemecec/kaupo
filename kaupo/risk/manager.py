"""Hard trading guardrails, independent of strategy code.

The risk manager sees every order intent before the venue and every fill
after it. It can reject intents, resize them, or halt the whole run.
Strategies cannot weaken these limits.

Buy sizing accounts for worst-case costs: the cash budget is deflated by
the taker fee + slippage so approved orders are always affordable.
"""

import enum
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from kaupo.domain import OrderIntent, Pair, Position, Side


class Decision(enum.Enum):
    APPROVED = "approved"
    RESIZED = "resized"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RiskConfig:
    max_position_quote: float = 1_000.0  # per-pair cap on position market value (magnitude)
    max_gross_exposure_quote: float = 2_000.0  # total across pairs (magnitudes)
    max_daily_loss_quote: float = 200.0  # halt when floor equity drops this much in a day (see _floor_equity)
    min_order_quote: float = 10.0  # below this, orders are rejected as dust
    max_consecutive_losses: int = 5  # then cooldown
    cooldown_candles: int = 12  # candles to wait after max_consecutive_losses
    leverage: float = 1.0  # 1x everywhere: spot, and perp fully-collateralized; >1 rejected
    instrument: str = "spot"  # "spot": long-only. "perp": shorts allowed (1x, funding charged)
    # worst-case costs used to deflate the cash budget (must match the venue)
    taker_fee_bps: float = 26.0
    slippage_bps: float = 5.0
    # cushion for adverse price movement between the decision candle's close
    # and the fill at the next candle's open (bigger moves -> ledger backstop)
    price_cushion_bps: float = 100.0

    def __post_init__(self) -> None:
        if self.leverage != 1.0:
            raise ValueError("Only 1x (leverage=1.0) is supported")
        if self.instrument not in ("spot", "perp"):
            raise ValueError(f"instrument must be 'spot' or 'perp', got {self.instrument!r}")

    @property
    def cost_rate(self) -> float:
        return (self.taker_fee_bps + self.slippage_bps + self.price_cushion_bps) / 10_000


@dataclass(frozen=True)
class RiskState:
    """What the risk manager needs to evaluate intents."""

    ts: datetime
    cash: float
    positions: dict[Pair, Position]
    prices: dict[Pair, float]  # last known price per pair
    equity: float


@dataclass
class Assessment:
    intent: OrderIntent
    decision: Decision
    reason: str = ""
    size: float = 0.0  # effective size after resizing


def _floor_equity(state: RiskState) -> float:
    """Equity floor for the daily rail: cash plus each open position valued at
    min(cost basis, market value).

    Givebacks of unrealized profit do not move the floor — a trend position
    that first appreciates cannot trip the rail. Real losses count fully:
    realized losses move cash down, and positions below cost show market value.
    """
    total = state.cash
    for pair, position in state.positions.items():
        market = position.market_value(state.prices.get(pair, 0.0))
        total += min(position.avg_entry * position.size, market)
    return total


@dataclass
class RiskManager:
    config: RiskConfig
    halted: bool = False
    halt_reason: str = ""
    _day: tuple[int, int, int] | None = None
    _day_start_floor: float = 0.0
    _consecutive_losses: int = 0
    _cooldown_remaining: int = 0
    rejections: deque[str] = field(default_factory=lambda: deque(maxlen=1000))

    def on_candle(self, state: RiskState) -> bool:
        """Advance time-based tracking. Returns True if the run may continue."""
        day = (state.ts.year, state.ts.month, state.ts.day)
        floor = _floor_equity(state)
        if day != self._day:
            self._day = day
            self._day_start_floor = floor

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1

        if floor - self._day_start_floor <= -self.config.max_daily_loss_quote:
            self.halted = True
            self.halt_reason = (
                f"max daily loss hit: floor equity {floor:.2f} vs day start {self._day_start_floor:.2f}"
            )
        return not self.halted

    def notify_trade_result(self, realized_pnl: float) -> None:
        """Called by the engine on each closing trade.

        A strictly negative PnL extends the loss streak; a positive PnL resets
        it; exactly zero leaves it unchanged.
        """
        if realized_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                self._cooldown_remaining = self.config.cooldown_candles
                self._consecutive_losses = 0
        elif realized_pnl > 0:
            self._consecutive_losses = 0

    def assess(self, intents: list[OrderIntent], state: RiskState) -> list[Assessment]:
        """Evaluate intents in order, folding each approval's effect (cash
        spent, exposure added) into the running state so caps hold across the
        whole batch, not just per intent."""
        if self.config.instrument == "perp":
            return self._assess_perp(intents, state)
        assessments: list[Assessment] = []
        cash_left = state.cash
        exposure_added: dict[Pair, float] = {}
        sold: dict[Pair, float] = {}
        for intent in intents:
            a = self._assess_one(intent, state, cash_left, exposure_added, sold)
            if a.decision is not Decision.REJECTED:
                price = intent.limit_price or state.prices.get(intent.pair, 0.0)
                if intent.side is Side.BUY:
                    cash_left -= a.size * price
                    exposure_added[intent.pair] = exposure_added.get(intent.pair, 0.0) + a.size * price
                else:
                    sold[intent.pair] = sold.get(intent.pair, 0.0) + a.size
            assessments.append(a)
        return assessments

    def _assess_one(
        self,
        intent: OrderIntent,
        state: RiskState,
        cash_left: float,
        exposure_added: dict[Pair, float],
        sold: dict[Pair, float],
    ) -> Assessment:
        cfg = self.config
        price = intent.limit_price or state.prices.get(intent.pair)
        if price is None or price <= 0:
            return self._reject(intent, "no price available")

        # cooldown gates new risk (BUYs); exits are always allowed through
        if self._cooldown_remaining > 0 and intent.side is Side.BUY:
            return self._reject(
                intent, f"cooldown after consecutive losses ({self._cooldown_remaining} left)"
            )

        position = state.positions.get(intent.pair, Position(pair=intent.pair))

        if intent.side is Side.SELL:
            remaining = position.size - sold.get(intent.pair, 0.0)
            size = min(intent.size, remaining)
            if size <= 0:
                return self._reject(intent, "no position to sell")
            # dust exits are allowed when they close the position entirely
            full_exit = size >= position.size
            if not full_exit and size * price < cfg.min_order_quote:
                return self._reject(intent, f"order value {size * price:.2f} below minimum")
            decision = Decision.RESIZED if size < intent.size else Decision.APPROVED
            return Assessment(
                intent=intent,
                decision=decision,
                size=size,
                reason="clamped to position" if decision is Decision.RESIZED else "",
            )

        # BUY: cap by per-pair limit, gross exposure, and affordable cash
        headroom_pair = (
            cfg.max_position_quote - position.market_value(price) - exposure_added.get(intent.pair, 0.0)
        )
        gross = sum(p.market_value(state.prices.get(pair, 0.0)) for pair, p in state.positions.items())
        gross += sum(exposure_added.values())
        headroom_gross = cfg.max_gross_exposure_quote - gross
        # deflate cash by worst-case costs so the fill is always affordable
        affordable = cash_left / (1 + cfg.cost_rate)
        budget = min(headroom_pair, headroom_gross, affordable)

        size = min(intent.size, budget / price) if budget > 0 else 0.0
        if size * price < cfg.min_order_quote:
            return self._reject(
                intent,
                f"order value {size * price:.2f} below minimum "
                f"(budget {budget:.2f}, pair headroom {headroom_pair:.2f}, "
                f"gross headroom {headroom_gross:.2f})",
            )
        decision = Decision.RESIZED if size < intent.size * 0.999 else Decision.APPROVED
        return Assessment(
            intent=intent,
            decision=decision,
            size=size,
            reason="clamped to risk budget" if decision is Decision.RESIZED else "",
        )

    def _reject(self, intent: OrderIntent, reason: str) -> Assessment:
        self.rejections.append(f"{intent.side.value} {intent.pair}: {reason}")
        return Assessment(intent=intent, decision=Decision.REJECTED, reason=reason)

    def _assess_perp(self, intents: list[OrderIntent], state: RiskState) -> list[Assessment]:
        """Perp intents: shorts allowed, caps hold on the RESULTING magnitude.

        Folds approvals into a running signed size per pair, so a batch
        cannot stack over the caps. Exposure-reducing deltas always pass the
        cap clamp (they move the size toward zero); the cooldown gates only
        magnitude-increasing intents. BUY cash needs are covered by the
        ledger backstop: at 1x with the exposure caps, the spot-style cash
        math stays sufficient.
        """
        assessments: list[Assessment] = []
        resulting: dict[Pair, float] = {pair: pos.size for pair, pos in state.positions.items()}
        for intent in intents:
            a = self._assess_perp_one(intent, state, resulting)
            if a.decision is not Decision.REJECTED:
                delta = a.size if intent.side is Side.BUY else -a.size
                resulting[intent.pair] = resulting.get(intent.pair, 0.0) + delta
            assessments.append(a)
        return assessments

    def _assess_perp_one(
        self,
        intent: OrderIntent,
        state: RiskState,
        resulting: dict[Pair, float],
    ) -> Assessment:
        cfg = self.config
        price = intent.limit_price or state.prices.get(intent.pair)
        if price is None or price <= 0:
            return self._reject(intent, "no price available")
        current = resulting.get(intent.pair, 0.0)
        delta = intent.size if intent.side is Side.BUY else -intent.size

        gross_other = sum(
            abs(resulting.get(pair, pos.size)) * state.prices.get(pair, 0.0)
            for pair in (set(resulting) | set(state.positions)) - {intent.pair}
            for pos in [state.positions.get(pair, Position(pair=pair))]
        )
        cap_quote = min(cfg.max_position_quote, cfg.max_gross_exposure_quote - gross_other)
        cap_size = max(cap_quote, 0.0) / price
        # the delta window that keeps |current + d| inside the caps
        d = min(max(delta, -cap_size - current), cap_size - current)
        if (delta > 0 and d <= 0) or (delta < 0 and d >= 0):
            return self._reject(intent, f"over exposure cap (room {cap_quote:.2f} quote)")
        increases = abs(current + d) > abs(current) + 1e-12
        if self._cooldown_remaining > 0 and increases:
            return self._reject(
                intent, f"cooldown after consecutive losses ({self._cooldown_remaining} left)"
            )
        size = abs(d)
        if size * price < cfg.min_order_quote and current + d != 0.0:
            # dust is only allowed when it closes the position entirely
            return self._reject(intent, f"order value {size * price:.2f} below minimum")
        decision = Decision.RESIZED if size < intent.size * 0.999 else Decision.APPROVED
        return Assessment(
            intent=intent,
            decision=decision,
            size=size,
            reason="clamped to exposure cap" if decision is Decision.RESIZED else "",
        )
