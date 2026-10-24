"""Currency identity shared by report readers."""

from kaupo.db.models import RunRow
from kaupo.domain import Pair


def run_quote_currency(run: RunRow) -> str | None:
    """Read the ledger denomination from a single pair or portfolio universe."""
    config = run.config or {}
    pairs = config.get("pairs")
    if pairs is None:
        pair = config.get("pair")
        pairs = pair.split(",") if isinstance(pair, str) else []
    if not isinstance(pairs, list) or not pairs:
        return None
    try:
        quotes = {Pair.parse(pair).quote for pair in pairs if isinstance(pair, str)}
        if len(quotes) == 1 and all(isinstance(pair, str) for pair in pairs):
            return quotes.pop()
    except ValueError:
        pass
    return None
