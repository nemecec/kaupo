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
    max_position_quote: float = 1_000.0  # per-pair cap on position market value
    max_gross_exposure_quote: float = 2_000.0  # total across pairs
    max_daily_loss_quote: float = 200.0  # halt when equity drops this much in a day
    min_order_quote: float = 10.0  # below this, orders are rejected as dust
    max_consecutive_losses: int = 5  # then cooldown
    cooldown_candles: int = 12  # candles to wait after max_consecutive_losses
    leverage: float = 1.0  # spot only; >1 rejected at construction
    # worst-case costs used to deflate the cash budget (must match the venue)
    taker_fee_bps: float = 26.0
    slippage_bps: float = 5.0
    # cushion for adverse price movement between the decision candle's close
    # and the fill at the next candle's open (bigger moves -> ledger backstop)
    price_cushion_bps: float = 100.0

    def __post_init__(self) -> None:
        if self.leverage != 1.0:
            raise ValueError("Only spot trading (leverage=1.0) is supported")

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


@dataclass
class RiskManager:
    config: RiskConfig
    halted: bool = False
    halt_reason: str = ""
    _day: tuple[int, int, int] | None = None
    _day_start_equity: float = 0.0
    _consecutive_losses: int = 0
    _cooldown_remaining: int = 0
    rejections: deque[str] = field(default_factory=lambda: deque(maxlen=1000))

    def on_candle(self, state: RiskState) -> bool:
        """Advance time-based tracking. Returns True if the run may continue."""
        day = (state.ts.year, state.ts.month, state.ts.day)
        if day != self._day:
            self._day = day
            self._day_start_equity = state.equity

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1

        if state.equity - self._day_start_equity <= -self.config.max_daily_loss_quote:
            self.halted = True
            self.halt_reason = (
                f"max daily loss hit: equity {state.equity:.2f} vs day start {self._day_start_equity:.2f}"
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
        assessments: list[Assessment] = []
        cash_left = state.cash
        exposure_added: dict[Pair, float] = {}
        for intent in intents:
            a = self._assess_one(intent, state, cash_left, exposure_added)
            if a.decision is not Decision.REJECTED:
                price = intent.limit_price or state.prices.get(intent.pair, 0.0)
                if intent.side is Side.BUY:
                    cash_left -= a.size * price
                    exposure_added[intent.pair] = exposure_added.get(intent.pair, 0.0) + a.size * price
            assessments.append(a)
        return assessments

    def _assess_one(
        self,
        intent: OrderIntent,
        state: RiskState,
        cash_left: float,
        exposure_added: dict[Pair, float],
    ) -> Assessment:
        cfg = self.config
        price = intent.limit_price or state.prices.get(intent.pair)
        if price is None or price <= 0:
            return self._reject(intent, "no price available")

        if self._cooldown_remaining > 0:
            return self._reject(
                intent, f"cooldown after consecutive losses ({self._cooldown_remaining} left)"
            )

        position = state.positions.get(intent.pair, Position(pair=intent.pair))

        if intent.side is Side.SELL:
            size = min(intent.size, position.size)
            if size <= 0:
                return self._reject(intent, "no position to sell")
            if size * price < cfg.min_order_quote:
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
