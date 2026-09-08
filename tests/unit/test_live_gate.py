"""Live trading is armed explicitly: a disarmed host places no order at all."""

import pytest

from kaupo.config import Settings
from kaupo.core.live_runner import LiveTradingUnavailable, check_armed
from kaupo.core.supervisor import live_start_error
from kaupo.data.assignments import Assignment
from kaupo.domain import RunMode, utc_now

# Placeholder credentials. Never a real key, in any test.
KEY = "test-key-placeholder"
SECRET = "test-secret-placeholder"  # noqa: S105 — a placeholder, not a credential


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "live_trading_enabled": True,
        "kraken_api_key": KEY,
        "kraken_api_secret": SECRET,
        "live_max_notional": 50.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def assignment(mode: str = RunMode.LIVE.value, pairs: list[str] | None = None) -> Assignment:
    now = utc_now()
    return Assignment(
        id="a1",
        strategy_id="maker-trend",
        pair="SOL/EUR" if pairs is None else ",".join(pairs),
        pairs=pairs,
        timeframe="4h",
        mode=mode,
        params={},
        enabled=True,
        starting_cash=1000.0,
        created_at=now,
        updated_at=now,
    )


class TestCheckArmed:
    def test_armed_and_credentialed_passes(self) -> None:
        check_armed(settings())

    def test_disabled_is_refused(self) -> None:
        with pytest.raises(LiveTradingUnavailable, match="disabled"):
            check_armed(settings(live_trading_enabled=False))

    def test_missing_credentials_are_refused(self) -> None:
        with pytest.raises(LiveTradingUnavailable, match="credentials"):
            check_armed(settings(kraken_api_key=""))
        with pytest.raises(LiveTradingUnavailable, match="credentials"):
            check_armed(settings(kraken_api_secret=""))

    def test_a_nonpositive_cap_is_refused(self) -> None:
        with pytest.raises(LiveTradingUnavailable, match="LIVE_MAX_NOTIONAL"):
            check_armed(settings(live_max_notional=0.0))

    def test_the_error_never_carries_the_credentials(self) -> None:
        with pytest.raises(LiveTradingUnavailable) as exc:
            check_armed(settings(live_trading_enabled=False))
        assert KEY not in str(exc.value)
        assert SECRET not in str(exc.value)


class TestSupervisorStartGate:
    def test_an_armed_single_pair_live_row_may_start(self) -> None:
        assert live_start_error(assignment(), settings()) is None

    def test_a_disarmed_host_blocks_the_row(self) -> None:
        blocked = live_start_error(assignment(), settings(live_trading_enabled=False))
        assert blocked is not None
        assert "disabled" in blocked

    def test_a_portfolio_live_row_is_rejected(self) -> None:
        blocked = live_start_error(assignment(pairs=["BTC/EUR", "SOL/EUR"]), settings())
        assert blocked is not None
        assert "single-pair" in blocked

    def test_shadow_rows_are_never_gated(self) -> None:
        disarmed = settings(live_trading_enabled=False, kraken_api_key="", kraken_api_secret="")
        assert live_start_error(assignment(mode=RunMode.SHADOW.value), disarmed) is None
        assert (
            live_start_error(assignment(mode=RunMode.SHADOW.value, pairs=["BTC/EUR", "SOL/EUR"]), disarmed)
            is None
        )


class TestDefaults:
    def test_live_is_off_out_of_the_box(self) -> None:
        defaults = Settings(_env_file=None)  # type: ignore[call-arg]
        assert defaults.live_trading_enabled is False
        assert defaults.kraken_api_key == ""
        assert defaults.kraken_api_secret == ""
        assert defaults.live_max_notional == 500.0
