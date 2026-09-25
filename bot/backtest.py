#!/usr/bin/env python3
"""Strategy backtest: DCA value strategy (paper only).

Replays the live paper-trader rules over historical daily data:
  - $100 paper contribution on the 1st of each calendar month ($50 start).
  - Buy $50 of a coin, at most twice per coin per calendar month, and only
    when the coin's price is below its 100-week (700-day) moving average.
    No buys until the coin has 700 days of history.
  - Sell a coin's entire position only when its price reaches 30% above that
    coin's average buy price (its DCA).
  - No leverage, no fees, cash earns nothing. Paper only: no real orders.

Also computes a buy-and-hold baseline that receives the same contributions
(split equally across coins) for comparison.

Data: Yahoo Finance daily closes (free, no key) for every window, since the
strategy needs 700 days of warmup history that CoinGecko's free tier cannot
serve. Pass --prices-file to replay a saved series instead.

Usage:
    python bot/backtest.py [--days 730] [--prices-file prices.json]
                           [--out backtest_results.json]
"""

import argparse
import bisect
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

COINS = ["bitcoin", "ethereum", "ripple", "bitcoin-cash", "kaspa",
         "pyth-network", "near", "bittensor", "tao-bot"]
STARTING_CASH = 50.0

YAHOO_SYMBOLS = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "ripple": "XRP-USD",
    "bitcoin-cash": "BCH-USD",
    "kaspa": "KAS-USD",
    "pyth-network": "PYTH-USD",
    "near": "NEAR-USD",
    "bittensor": "TAO22974-USD",
    "tao-bot": "TAOBOT-USD",
}

BUY_USD = float(os.getenv("BUY_USD", "50"))
MAX_BUYS_PER_MONTH = int(os.getenv("MAX_BUYS_PER_MONTH", "2"))
MA_DAYS = int(os.getenv("MA_DAYS", "700"))          # 100-week moving average
TAKE_PROFIT_MULT = float(os.getenv("TAKE_PROFIT_MULT", "1.30"))
MONTHLY_CONTRIB = float(os.getenv("MONTHLY_CONTRIB", "100"))


def fetch_history(days):
    """Daily closes for the window plus MA_DAYS of warmup, per coin."""
    span = days + MA_DAYS + 10
    to_s = int(time.time())
    from_s = to_s - span * 86400
    series = {}
    for i, cid in enumerate(COINS):
        sym = YAHOO_SYMBOLS[cid]
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?interval=1d&period1={from_s}&period2={to_s}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())["chart"]["result"][0]
        stamps = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        pts = sorted(
            (datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), c)
            for t, c in zip(stamps, closes) if c is not None)
        # de-dupe (keep last)
        dedup = {}
        for d, c in pts:
            dedup[d] = c
        series[cid] = sorted(dedup.items())
        print(f"  {cid}: {len(series[cid])} daily closes", flush=True)
        if i < len(COINS) - 1:
            time.sleep(2)
    return series


def run_backtest(full_series, days):
    dates = {c: [d for d, _ in full_series[c]] for c in COINS}
    closes = {c: [p for _, p in full_series[c]] for c in COINS}

    end_dt = max(datetime.strptime(d, "%Y-%m-%d")
                 for c in COINS for d in dates[c])
    grid = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)]

    def price_at(c, day):
        i = bisect.bisect_right(dates[c], day) - 1
        return closes[c][i] if i >= 0 else None

    def ma_at(c, day):
        i = bisect.bisect_right(dates[c], day) - 1
        if i < MA_DAYS - 1:
            return None
        window = closes[c][i - MA_DAYS + 1:i + 1]
        return sum(window) / MA_DAYS

    cash = STARTING_CASH
    contributed = STARTING_CASH
    pos = {c: {"units": 0.0, "invested": 0.0,
               "buy_month": None, "buy_count": 0} for c in COINS}
    trades = []
    equity_curve = []

    for day in grid:
        month = day[:7]
        # 1) monthly contribution on the 1st
        if day.endswith("-01"):
            cash += MONTHLY_CONTRIB
            contributed += MONTHLY_CONTRIB
        # 2) sells: full exit at +30% over the coin's DCA
        for c in COINS:
            p = pos[c]
            if p["units"] <= 0:
                continue
            price = price_at(c, day)
            if price is None:
                continue
            dca = p["invested"] / p["units"]
            if price >= dca * TAKE_PROFIT_MULT:
                proceeds = p["units"] * price
                pnl = proceeds - p["invested"]
                cash += proceeds
                trades.append({"coin": c, "side": "sell", "date": day,
                               "price": round(price, 4),
                               "dca": round(dca, 4),
                               "proceeds": round(proceeds, 2),
                               "pnl": round(pnl, 2)})
                pos[c] = {"units": 0.0, "invested": 0.0,
                          "buy_month": p["buy_month"],
                          "buy_count": p["buy_count"]}
        # 3) buys: $50 below the 100-week MA, max 2 per coin per month
        for c in COINS:
            price = price_at(c, day)
            if price is None:
                continue
            p = pos[c]
            if p["buy_month"] != month:
                p["buy_month"] = month
                p["buy_count"] = 0
            ma = ma_at(c, day)
            if (ma is not None and price < ma
                    and p["buy_count"] < MAX_BUYS_PER_MONTH
                    and cash >= BUY_USD):
                units = BUY_USD / price
                p["units"] += units
                p["invested"] += BUY_USD
                p["buy_count"] += 1
                cash -= BUY_USD
                trades.append({"coin": c, "side": "buy", "date": day,
                               "price": round(price, 4),
                               "amount": BUY_USD,
                               "dca": round(p["invested"] / p["units"], 4)})
        # 4) equity (open positions marked to market)
        equity = cash + sum(pos[c]["units"] * (price_at(c, day) or 0)
                            for c in COINS)
        equity_curve.append((day, round(equity, 2)))

    return trades, equity_curve, contributed


def baseline(full_series, days, contributed_schedule):
    """Buy-and-hold receiving the same contributions, split equally."""
    dates = {c: [d for d, _ in full_series[c]] for c in COINS}
    closes = {c: [p for _, p in full_series[c]] for c in COINS}

    def price_at(c, day):
        i = bisect.bisect_right(dates[c], day) - 1
        return closes[c][i] if i >= 0 else None

    end_dt = max(datetime.strptime(d, "%Y-%m-%d")
                 for c in COINS for d in dates[c])
    grid = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)]
    units = {c: 0.0 for c in COINS}
    per = STARTING_CASH / len(COINS)
    for c in COINS:
        p0 = price_at(c, grid[0])
        if p0:
            units[c] += per / p0
    for day, amount in contributed_schedule:
        per = amount / len(COINS)
        for c in COINS:
            px = price_at(c, day)
            if px:
                units[c] += per / px
    total, per_coin = 0.0, {}
    for c in COINS:
        last = price_at(c, grid[-1]) or 0
        val = units[c] * last
        per_coin[c] = round(val, 2)
        total += val
    return round(total, 2), per_coin


def monthly_returns(equity_curve):
    months = {}
    for day, eq in equity_curve:
        months.setdefault(day[:7], []).append(eq)
    out = {}
    for m, eqs in sorted(months.items()):
        out[m] = round((eqs[-1] / eqs[0] - 1) * 100, 2) if eqs[0] else 0.0
    return out


def max_drawdown(equity_curve):
    peak, dd = equity_curve[0][1], 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak * 100)
    return round(dd, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--prices-file", default=None)
    ap.add_argument("--out", default="backtest_results.json")
    args = ap.parse_args()

    if args.prices_file:
        with open(args.prices_file) as f:
            raw = json.load(f)
        full = {c: sorted((p["date"], p["price"]) for p in raw["coins"][c])
                for c in COINS}
        source = f"local file {args.prices_file}"
    else:
        print(f"Fetching {args.days}d window + {MA_DAYS}d warmup from Yahoo Finance...")
        full = fetch_history(args.days)
        source = "Yahoo Finance daily closes, vs USD"

    trades, equity, contributed = run_backtest(full, args.days)

    end_dt = max(datetime.strptime(d, "%Y-%m-%d")
                 for c in COINS for d, _ in full[c])
    grid_start = (end_dt - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    grid = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(args.days - 1, -1, -1)]
    contrib_schedule = [(d, MONTHLY_CONTRIB) for d in grid if d.endswith("-01")]
    base_total, base_per = baseline(full, args.days, contrib_schedule)

    months = monthly_returns(equity)
    end_balance = equity[-1][1]
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    per_coin_pnl = {c: round(sum(t.get("pnl", 0) for t in sells
                                 if t["coin"] == c), 2) for c in COINS}

    result = {
        "strategy": (f"DCA value: ${BUY_USD:.0f} buys (max {MAX_BUYS_PER_MONTH}/coin/month) "
                     f"only below the {MA_DAYS}-day moving average, "
                     f"${MONTHLY_CONTRIB:.0f}/month contributions, "
                     f"sell full position at +{round((TAKE_PROFIT_MULT - 1) * 100)}% over coin DCA"),
        "source": source,
        "window": {"start": equity[0][0], "end": equity[-1][0],
                   "days": len(equity)},
        "starting_cash": STARTING_CASH,
        "total_contributed": round(contributed, 2),
        "ending_balance": end_balance,
        "total_return_pct": round((end_balance / contributed - 1) * 100, 2),
        "buys": len(buys),
        "sells": len(sells),
        "win_rate_pct": round(wins / len(sells) * 100, 1) if sells else 0.0,
        "per_coin_pnl": per_coin_pnl,
        "best_month": max(months.items(), key=lambda kv: kv[1]) if months else None,
        "worst_month": min(months.items(), key=lambda kv: kv[1]) if months else None,
        "max_drawdown_pct": max_drawdown(equity),
        "baseline_buy_and_hold": base_total,
        "baseline_per_coin": base_per,
        "baseline_return_pct": round((base_total / contributed - 1) * 100, 2),
        "disclaimer": ("Simulated past performance does not predict future "
                       "results. Educational only, not financial advice."),
    }

    with open(args.out, "w") as f:
        json.dump({**result, "trade_ledger": trades}, f, indent=2)

    print(f"Window: {result['window']['start']} -> {result['window']['end']} "
          f"({len(equity)} days)")
    print(f"Strategy: {result['strategy']}")
    print(f"Contributed: ${contributed:,.2f}")
    print(f"Ending balance: ${end_balance:,.2f} "
          f"({result['total_return_pct']:+.1f}% on contributed)")
    print(f"Buys: {len(buys)}, sells: {len(sells)}, "
          f"sell win rate: {result['win_rate_pct']}%")
    print(f"Max drawdown: {result['max_drawdown_pct']}%")
    print(f"Buy-and-hold baseline: ${base_total:,.2f} "
          f"({result['baseline_return_pct']:+.1f}%)")
    print(f"Results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
