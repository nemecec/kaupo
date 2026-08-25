"""Loader: portfolio strategies register alongside single-pair ones."""

import textwrap
from pathlib import Path

import pytest

from kaupo.sdk.loader import load_strategies
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase

PORTFOLIO = textwrap.dedent(
    """
    from pydantic import BaseModel
    from kaupo.sdk.protocol import PortfolioStrategyBase

    class Params(BaseModel):
        top_k: int = 3

    class Rotator(PortfolioStrategyBase):
        id = "rotator"
        params_schema = Params

        def on_candle(self, ctx):
            return []
    """
)

SINGLE = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase

    class Single(StrategyBase):
        id = "single"
        def on_candle(self, ctx):
            return []
    """
)


def test_portfolio_strategy_loads_and_creates(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(PORTFOLIO)
    loaded = load_strategies(tmp_path)
    strat = loaded["rotator"]
    assert strat.is_portfolio
    assert issubclass(strat.cls, PortfolioStrategyBase)
    assert not issubclass(strat.cls, StrategyBase)
    instance = strat.create({"top_k": 5})
    assert isinstance(instance, PortfolioStrategyBase)
    assert instance.params.top_k == 5  # type: ignore[attr-defined]


def test_both_kinds_register_from_one_directory(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(PORTFOLIO)
    (tmp_path / "s.py").write_text(SINGLE)
    loaded = load_strategies(tmp_path)
    assert set(loaded) == {"rotator", "single"}
    assert loaded["rotator"].is_portfolio
    assert not loaded["single"].is_portfolio


def test_duplicate_id_across_kinds_rejected(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(PORTFOLIO)
    (tmp_path / "clash.py").write_text(SINGLE.replace('"single"', '"rotator"').replace("Single", "Clash"))
    with pytest.raises(ValueError, match="Duplicate strategy id"):
        load_strategies(tmp_path)


def test_both_kinds_in_one_file(tmp_path: Path) -> None:
    (tmp_path / "both.py").write_text(PORTFOLIO + SINGLE)
    loaded = load_strategies(tmp_path)
    assert set(loaded) == {"rotator", "single"}
