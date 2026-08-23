"""Hard trading guardrails, independent of strategy code.

The risk manager sees every order intent before the venue and every fill
after it. It can reject intents, resize them, or halt the whole run.
Strategies cannot weaken these limits.
"""

import enum
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

    def __post_init__(self) -> None:
        if self.leverage != 1.0:
            raise ValueError("Only spot trading (leverage=1.0) is supported")


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
    rejections: list[str] = field(default_factory=list)

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
        """Called by the engine on each closing trade."""
        if realized_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                self._cooldown_remaining = self.config.cooldown_candles
                self._consecutive_losses = 0
        else:
            self._consecutive_losses = 0

    def assess(self, intents: list[OrderIntent], state: RiskState) -> list[Assessment]:
        return [self._assess_one(i, state) for i in intents]

    def _assess_one(self, intent: OrderIntent, state: RiskState) -> Assessment:
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

        # BUY: cap by per-pair limit, gross exposure, and available cash
        headroom_pair = cfg.max_position_quote - position.market_value(price)
        gross = sum(p.market_value(state.prices.get(pair, 0.0)) for pair, p in state.positions.items())
        headroom_gross = cfg.max_gross_exposure_quote - gross
        budget = min(headroom_pair, headroom_gross, state.cash * 0.999)  # keep a fee buffer

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
