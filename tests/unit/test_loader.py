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


def test_missing_id_file_skipped_with_error(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from kaupo.sdk.protocol import StrategyBase\n"
        "class NoId(StrategyBase):\n"
        "    def on_candle(self, ctx): return []\n"
    )
    (tmp_path / "ok.py").write_text(VALID)
    loaded = load_strategies(tmp_path)
    # the broken file is skipped (logged), healthy strategies still load
    assert "my-strat" in loaded
    assert len(loaded) == 1


def test_missing_directory() -> None:
    with pytest.raises(FileNotFoundError):
        load_strategies(Path("/nonexistent/dir"))


def test_import_time_crash_isolated(tmp_path: Path) -> None:
    (tmp_path / "boom.py").write_text("X = 1 / 0\n")
    (tmp_path / "ok.py").write_text(VALID)
    loaded = load_strategies(tmp_path)
    assert list(loaded) == ["my-strat"]


def test_unknown_param_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(VALID)
    strat = load_strategies(tmp_path)["my-strat"]
    with pytest.raises(ValueError, match="Unknown params"):
        strat.create({"threshhold": 0.5})  # typo


def test_alias_params_accepted(tmp_path: Path) -> None:
    (tmp_path / "alias.py").write_text(
        "from pydantic import BaseModel, Field\n"
        "from kaupo.sdk.protocol import StrategyBase\n\n"
        "class P(BaseModel):\n"
        "    threshold: float = Field(default=0.5, alias='th')\n\n"
        "class S(StrategyBase):\n"
        "    id = 'alias-strat'\n"
        "    params_schema = P\n"
        "    def on_candle(self, ctx): return []\n"
    )
    strat = load_strategies(tmp_path)["alias-strat"]
    instance = strat.create({"th": 0.9})
    assert instance.params.threshold == 0.9  # type: ignore[attr-defined]
