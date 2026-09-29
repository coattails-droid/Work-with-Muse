#!/usr/bin/env python3
"""Multi-source price fetching with automatic fallback.

CoinGecko is the primary source, retried with backoff. When it refuses
(e.g. HTTP 403 from shared GitHub runner IPs), the fetch falls through to
keyless fallbacks and merges coverage: a coin is covered as soon as ANY
source returns it.

Fallbacks (all free, no API key):
  1. CoinPaprika - covers all tracked coins; 24h % change given directly.
  2. Kraken      - covers 7/9 (no KAS, no TAOBOT); 24h change is derived
                   from today's 00:00 UTC open, which is close enough for
                   alerting and paper-trade decisions.

Public API:
  get_prices(coin_ids) -> (prices, sources)
      prices:  {coingecko_id: {"usd": float, "usd_24h_change": float|None}}
      sources: {coingecko_id: "coingecko"|"coinpaprika"|"kraken"}
  get_markets(coin_ids) -> [CoinGecko-coins/markets-shaped dicts]
      for price_sentinel: id, symbol, name, current_price, high_24h,
      low_24h, price_change_percentage_24h. Coins with no data from any
      source are simply absent.

Neither function raises for HTTP/API failures; they return whatever could
be gathered (possibly empty) and log per-source failures to stderr. Callers
decide how to degrade: paper_trader skips coins without prices, and both
bots exit 0 (clean skip, no failure alert) when nothing could be fetched.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "crypto-paper-bot/1.0"}

# Static display metadata (CoinGecko ids) so fallback sources can build
# sentinel-shaped market dicts without CoinGecko.
COIN_META = {
    "bitcoin":      {"symbol": "BTC",    "name": "Bitcoin"},
    "ethereum":     {"symbol": "ETH",    "name": "Ethereum"},
    "ripple":       {"symbol": "XRP",    "name": "XRP"},
    "bitcoin-cash": {"symbol": "BCH",    "name": "Bitcoin Cash"},
    "kaspa":        {"symbol": "KAS",    "name": "Kaspa"},
    "pyth-network": {"symbol": "PYTH",   "name": "Pyth Network"},
    "near":         {"symbol": "NEAR",   "name": "NEAR Protocol"},
    "bittensor":    {"symbol": "TAO",    "name": "Bittensor"},
    "tao-bot":      {"symbol": "TAOBOT", "name": "tao.bot"},
}

# CoinGecko id -> CoinPaprika id (verified live 2026-09-29)
PAPRIKA_IDS = {
    "bitcoin": "btc-bitcoin",
    "ethereum": "eth-ethereum",
    "ripple": "xrp-xrp",
    "bitcoin-cash": "bch-bitcoin-cash",
    "kaspa": "kas-kaspa",
    "pyth-network": "pyth-pyth-network",
    "near": "near-near-protocol",
    "bittensor": "tao-bittensor",
    "tao-bot": "taobot-taobot",
}

# CoinGecko id -> Kraken pair (verified live 2026-09-29; KAS/TAOBOT unlisted)
KRAKEN_PAIRS = {
    "bitcoin": "XBTUSD",
    "ethereum": "ETHUSD",
    "ripple": "XRPUSD",
    "bitcoin-cash": "BCHUSD",
    "pyth-network": "PYTHUSD",
    "near": "NEARUSD",
    "bittensor": "TAOUSD",
}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fetch_coingecko(coin_ids):
    """Primary source. Retried with backoff; {} on persistent failure."""
    ids = ",".join(coin_ids)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={urllib.parse.quote(ids)}&vs_currencies=usd"
           "&include_24hr_change=true")
    last_err = None
    for attempt, wait in ((1, 0), (2, 2), (3, 5)):
        try:
            if wait:
                time.sleep(wait)
            data = _get(url)
            out = {}
            for cid in coin_ids:
                v = data.get(cid) or {}
                if "usd" in v:
                    out[cid] = {"usd": float(v["usd"]),
                                "usd_24h_change": v.get("usd_24h_change"),
                                "high_24h": None, "low_24h": None}
            return out
        except Exception as e:
            last_err = e
    print(f"  [prices] coingecko failed after retries: {last_err}",
          file=sys.stderr)
    return {}


def _fetch_coinpaprika(coin_ids):
    """Fallback 1: per-coin tickers. {} if nothing could be fetched."""
    out = {}
    for cid in coin_ids:
        pid = PAPRIKA_IDS.get(cid)
        if not pid:
            continue
        try:
            d = _get(f"https://api.coinpaprika.com/v1/tickers/{pid}"
                     "?quotes=USD")
            q = d["quotes"]["USD"]
            out[cid] = {"usd": float(q["price"]),
                        "usd_24h_change": q.get("percent_change_24h"),
                        "high_24h": None, "low_24h": None}
        except Exception as e:
            print(f"  [prices] coinpaprika {cid}: {e}", file=sys.stderr)
    if not out:
        print("  [prices] coinpaprika: no coins fetched", file=sys.stderr)
    return out


def _fetch_kraken(coin_ids):
    """Fallback 2: one Ticker call per pair. 24h change vs today's open."""
    out = {}
    for cid in coin_ids:
        pair = KRAKEN_PAIRS.get(cid)
        if not pair:
            continue
        try:
            d = _get("https://api.kraken.com/0/public/Ticker?pair=" + pair)
            res = d.get("result") or {}
            if not res:
                raise RuntimeError(f"api error: {d.get('error')}")
            t = next(iter(res.values()))
            last = float(t["c"][0])
            today_open = float(t["o"])
            chg = ((last - today_open) / today_open * 100
                   if today_open else None)
            out[cid] = {"usd": last, "usd_24h_change": chg,
                        "high_24h": float(t["h"][0]),
                        "low_24h": float(t["l"][0])}
        except Exception as e:
            print(f"  [prices] kraken {cid}: {e}", file=sys.stderr)
    if not out:
        print("  [prices] kraken: no coins fetched", file=sys.stderr)
    return out


SOURCES = (
    ("coingecko", _fetch_coingecko),
    ("coinpaprika", _fetch_coinpaprika),
    ("kraken", _fetch_kraken),
)


def get_prices(coin_ids):
    """Merged per-coin prices from the first sources that have them.

    Returns (prices, sources). Never raises for source failures.
    """
    prices, sources = {}, {}
    for name, fn in SOURCES:
        missing = [c for c in coin_ids if c not in prices]
        if not missing:
            break
        try:
            got = fn(missing)
        except Exception as e:  # defensive: a source must never kill the run
            print(f"  [prices] {name} crashed: {e}", file=sys.stderr)
            got = {}
        for cid, rec in got.items():
            if cid not in prices:
                prices[cid] = rec
                sources[cid] = name
    return prices, sources


def get_markets(coin_ids):
    """CoinGecko coins/markets-shaped list built from merged sources."""
    prices, sources = get_prices(coin_ids)
    markets = []
    for cid in coin_ids:
        rec = prices.get(cid)
        if not rec:
            continue
        meta = COIN_META.get(cid, {"symbol": cid.upper(), "name": cid})
        markets.append({
            "id": cid,
            "symbol": meta["symbol"],
            "name": meta["name"],
            "current_price": rec["usd"],
            "high_24h": rec.get("high_24h"),
            "low_24h": rec.get("low_24h"),
            "price_change_percentage_24h": rec.get("usd_24h_change"),
            "_source": sources[cid],
        })
    return markets
