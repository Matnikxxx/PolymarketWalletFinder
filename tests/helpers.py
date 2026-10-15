"""
Plain (non-pytest) test helpers shared across the test suite.
"""


def make_event(
    wallet="0xabc0000000000000000000000000000000000a",
    side="BUY",
    price=0.3,
    size=100.0,
    condition_id="cond-1",
    token_id="tok-1",
    slug="market-a",
    market_title="Market A",
    outcome="Yes",
    timestamp="2026-09-01T00:00:00+00:00",
    tx_hash=None,
    event_id=None,
):
    ev = {
        "wallet": wallet.lower(),
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": price * size,
        "condition_id": condition_id,
        "token_id": token_id,
        "slug": slug,
        "market_title": market_title,
        "outcome": outcome,
        "timestamp": timestamp,
        "tx_hash": tx_hash,
    }
    from polymarket.wallet_activity import event_id as gen_event_id
    ev["event_id"] = event_id or gen_event_id(ev)
    return ev
