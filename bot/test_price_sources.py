#!/usr/bin/env python3
"""Tests for bot/price_sources.py. No network: _get and source fetchers are
stubbed. Run: python bot/test_price_sources.py (from repo root)."""

import sys

sys.path.insert(0, "bot")
import price_sources as ps


def stub_get_factory(responses):
    """Return a fake _get serving canned responses per URL substring."""
    def fake_get(url, timeout=15):
        for key, payload in responses.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected URL: {url}")
    return fake_get


def test_coingecko_parse():
    ps._get = stub_get_factory({
        "simple/price": {
            "bitcoin": {"usd": 84000.5, "usd_24h_change": 1.25},
            "ethereum": {"usd": 2700.0, "usd_24h_change": -0.5},
        }
    })
    out = ps._fetch_coingecko(["bitcoin", "ethereum"])
    assert out["bitcoin"] == {"usd": 84000.5, "usd_24h_change": 1.25,
                              "high_24h": None, "low_24h": None}, out
    assert out["ethereum"]["usd_24h_change"] == -0.5
    print("ok coingecko parse")


def test_coingecko_persistent_failure_no_raise():
    err = IOError("HTTP Error 403: Forbidden")
    ps._get = stub_get_factory({"simple/price": err})
    ps.time.sleep = lambda s: None  # skip retry backoff in tests
    try:
        out = ps._fetch_coingecko(["bitcoin"])
    finally:
        import importlib
        importlib.reload(ps)
    assert out == {}, out
    print("ok coingecko persistent failure returns {} without raising")


def test_coinpaprika_parse():
    ps._get = stub_get_factory({
        "tickers/kas-kaspa": {"id": "kas-kaspa", "name": "Kaspa",
                              "symbol": "KAS",
                              "quotes": {"USD": {"price": 0.045,
                                                 "percent_change_24h": -3.7}}},
    })
    out = ps._fetch_coinpaprika(["kaspa"])
    rec = out["kaspa"]
    assert rec["usd"] == 0.045 and rec["usd_24h_change"] == -3.7, rec
    print("ok coinpaprika parse")


def test_kraken_parse():
    ps._get = stub_get_factory({
        "Ticker?pair=XBTUSD": {"error": [],
                               "result": {"XXBTZUSD": {
                                   "c": ["84110.1", "0.5"],
                                   "o": "83462.7",
                                   "h": ["85000.0", "1.0"],
                                   "l": ["83000.0", "1.0"]}}},
    })
    out = ps._fetch_kraken(["bitcoin"])
    rec = out["bitcoin"]
    assert rec["usd"] == 84110.1, rec
    expect = (84110.1 - 83462.7) / 83462.7 * 100
    assert abs(rec["usd_24h_change"] - expect) < 1e-9, rec
    assert rec["high_24h"] == 85000.0 and rec["low_24h"] == 83000.0, rec
    print("ok kraken parse (24h change vs today's open)")


def test_merge_first_source_wins():
    def cg(ids):
        return {"bitcoin": {"usd": 84000.0, "usd_24h_change": 1.0,
                            "high_24h": None, "low_24h": None}}
    def pp(ids):
        assert ids == ["ethereum", "kaspa"], ids  # only asked for the missing
        return {"bitcoin": {"usd": 1.0, "usd_24h_change": 0.0,
                            "high_24h": None, "low_24h": None},
                "ethereum": {"usd": 2700.0, "usd_24h_change": 2.0,
                             "high_24h": None, "low_24h": None},
                "kaspa": {"usd": 0.045, "usd_24h_change": -3.0,
                          "high_24h": None, "low_24h": None}}
    def kr(ids):
        raise AssertionError("kraken should not be called; all covered")
    ps.SOURCES = (("coingecko", cg), ("coinpaprika", pp), ("kraken", kr))
    try:
        prices, sources = ps.get_prices(["bitcoin", "ethereum", "kaspa"])
    finally:
        _restore_sources()
    assert set(prices) == {"bitcoin", "ethereum", "kaspa"}, prices
    assert prices["bitcoin"]["usd"] == 84000.0  # coingecko wins, not paprika
    assert sources == {"bitcoin": "coingecko", "ethereum": "coinpaprika",
                       "kaspa": "coinpaprika"}, sources
    print("ok merge: union coverage, first source wins, sources tracked")


def test_crashing_source_does_not_kill_chain():
    def boom(ids):
        raise RuntimeError("kaput")
    def pp(ids):
        return {"bitcoin": {"usd": 84000.0, "usd_24h_change": 1.0,
                            "high_24h": None, "low_24h": None}}
    ps.SOURCES = (("coingecko", boom), ("coinpaprika", pp))
    try:
        prices, sources = ps.get_prices(["bitcoin"])
    finally:
        _restore_sources()
    assert prices["bitcoin"]["usd"] == 84000.0
    assert sources["bitcoin"] == "coinpaprika"
    print("ok crashing source is skipped, chain continues")


def test_total_outage_returns_empty_no_raise():
    ps.SOURCES = tuple((n, lambda ids: {}) for n, _ in ps.SOURCES)
    try:
        prices, sources = ps.get_prices(["bitcoin", "kaspa"])
        markets = ps.get_markets(["bitcoin", "kaspa"])
    finally:
        _restore_sources()
    assert prices == {} and sources == {}, (prices, sources)
    assert markets == [], markets
    print("ok total outage: empty results, no exception")


def test_get_markets_shape():
    def pp(ids):
        return {c: {"usd": 100.0, "usd_24h_change": 5.5,
                    "high_24h": None, "low_24h": None} for c in ids}
    ps.SOURCES = (("coinpaprika", pp),)
    try:
        markets = ps.get_markets(["kaspa", "tao-bot"])
    finally:
        _restore_sources()
    assert len(markets) == 2
    for m in markets:
        for k in ("id", "symbol", "name", "current_price", "high_24h",
                  "low_24h", "price_change_percentage_24h", "_source"):
            assert k in m, (k, m)
    kas = next(m for m in markets if m["id"] == "kaspa")
    assert (kas["symbol"], kas["name"]) == ("KAS", "Kaspa"), kas
    assert kas["price_change_percentage_24h"] == 5.5
    print("ok get_markets sentinel-shaped dicts")


def _restore_sources():
    import importlib
    importlib.reload(ps)


if __name__ == "__main__":
    # reload-based restore needs a fresh module each time; run sequentially
    test_coingecko_parse()
    test_coingecko_persistent_failure_no_raise()
    test_coinpaprika_parse()
    test_kraken_parse()
    test_merge_first_source_wins()
    test_crashing_source_does_not_kill_chain()
    test_total_outage_returns_empty_no_raise()
    test_get_markets_shape()
    print("all price_sources tests passed")
