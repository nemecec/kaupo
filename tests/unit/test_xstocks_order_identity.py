from copy import deepcopy

import pytest

from kaupo.venues.xstocks_order_identity import check_order_fill_identities

ROW = {
    "aclass_base": "tokenized_asset",
    "aclass_quote": "currency",
    "base": "AAPLx",
    "quote": "ZUSD",
    "altname": "AAPLxUSD",
    "wsname": "AAPLx/USD",
}
METADATA = {
    "AAPLSPVUSD": ROW,
    "TSLASPVUSD": {**ROW, "base": "TSLAx", "altname": "TSLAxUSD", "wsname": "TSLAx/USD"},
}
ORDER = {
    "status": "expired",
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit"},
    "vol": "2",
    "vol_exec": "0.5",
    "expiretm": 200,
    "opentm": 100,
    "closetm": 200,
    "trades": ["t1"],
}
FILL = {"ordertxid": "o1", "pair": "AAPLSPVUSD"}


@pytest.mark.parametrize("order_pair", ["AAPLxUSD", "AAPLx/USD", "AAPLSPVUSD"])
@pytest.mark.parametrize("fill_pair", ["AAPLxUSD", "AAPLx/USD", "AAPLSPVUSD"])
def test_exact_aliases_map_without_mutating(order_pair, fill_pair):
    orders = {"o1": {**ORDER, "descr": {**ORDER["descr"], "pair": order_pair}}}
    fills = {"t1": {**FILL, "pair": fill_pair}}
    before = deepcopy((orders, fills, METADATA))
    report = check_order_fill_identities(orders, fills, METADATA)
    assert report["status"] == "pairs_match"
    assert report["orders"]["o1"]["mapped_pair"] == "AAPLX/USD"
    assert report["orders"]["o1"]["fills"]["t1"]["mapped_pair"] == "AAPLX/USD"
    assert (orders, fills, METADATA) == before


@pytest.mark.parametrize(
    "order_pair,fill_pair,reason",
    [
        ("AAPLXUSD", "AAPLSPVUSD", "unmapped_order_pair"),
        ("AAPLxUSD", "AAPLXUSD", "unmapped_fill_pair"),
        ("AAPLxUSD", "TSLASPVUSD", "order_fill_pair_mismatch"),
        ("AAPL", "AAPL", "unmapped_order_pair"),
    ],
)
def test_unknown_or_different_instrument_requires_review(order_pair, fill_pair, reason):
    orders = {"o1": {**ORDER, "descr": {**ORDER["descr"], "pair": order_pair}}}
    report = check_order_fill_identities(orders, {"t1": {**FILL, "pair": fill_pair}}, METADATA)
    assert report["status"] == "needs_review"
    assert report["orders"]["o1"]["fills"]["t1"]["reason"] == reason


def test_mixed_orders_do_not_hide_mismatch():
    orders = {"o1": ORDER, "o2": {**ORDER, "trades": ["t2"]}}
    fills = {"t1": FILL, "t2": {"ordertxid": "o2", "pair": "TSLASPVUSD"}}
    report = check_order_fill_identities(orders, fills, METADATA)
    assert report["status"] == "needs_review"
    assert report["orders"]["o1"]["status"] == "pairs_match"
    assert report["orders"]["o2"]["status"] == "needs_review"


def test_no_executions_are_not_identity_evidence():
    report = check_order_fill_identities({"o1": {**ORDER, "trades": [], "vol_exec": "0"}}, {}, {})
    assert report["status"] == "no_fills"
    assert report["orders"]["o1"]["mapped_pair"] is None


def test_wrong_owner_and_missing_references():
    report = check_order_fill_identities({"o1": ORDER}, {"t1": {**FILL, "ordertxid": "other"}}, METADATA)
    assert report["orders"]["o1"]["fills"]["t1"]["reason"] == "fill_order_mismatch"
    with pytest.raises(ValueError, match="exactly"):
        check_order_fill_identities({"o1": ORDER}, {}, METADATA)
