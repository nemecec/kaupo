import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaupo.sdk.loader import load_strategies
from kaupo.sdk.protocol import StrategyBase

VALID = textwrap.dedent(
    """
    from pydantic import BaseModel
    from kaupo.sdk.protocol import StrategyBase

    class Params(BaseModel):
        threshold: float = 0.5

    class MyStrategy(StrategyBase):
        id = "my-strat"
        params_schema = Params

        def on_candle(self, ctx):
            return []
    """
)

OTHER = VALID.replace('"my-strat"', '"other-strat"').replace("MyStrategy", "OtherStrategy")


def test_load_strategies(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(VALID)
    (tmp_path / "b.py").write_text(OTHER)
    (tmp_path / "_helper.py").write_text("X = 1  # skipped")

    loaded = load_strategies(tmp_path)
    assert set(loaded) == {"my-strat", "other-strat"}

    strat = loaded["my-strat"]
    assert issubclass(strat.cls, StrategyBase)
    assert len(strat.source_hash) == 64
    assert strat.version == strat.source_hash[:12]


def test_create_validates_params(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(VALID)
    strat = load_strategies(tmp_path)["my-strat"]

    instance = strat.create({"threshold": 0.9})
    assert instance.params.threshold == 0.9  # type: ignore[attr-defined]

    with pytest.raises(ValidationError):
        strat.create({"threshold": "not-a-number"})


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(VALID)
    (tmp_path / "copy.py").write_text(VALID)
    with pytest.raises(ValueError, match="Duplicate strategy id"):
        load_strategies(tmp_path)


def test_missing_id_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from kaupo.sdk.protocol import StrategyBase\n"
        "class NoId(StrategyBase):\n"
        "    def on_candle(self, ctx): return []\n"
    )
    with pytest.raises(ValueError, match="no 'id'"):
        load_strategies(tmp_path)


def test_missing_directory() -> None:
    with pytest.raises(FileNotFoundError):
        load_strategies(Path("/nonexistent/dir"))
