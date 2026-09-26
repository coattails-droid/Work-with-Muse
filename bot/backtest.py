#!/usr/bin/env python3
"""Strategy backtest: DCA value strategy (paper only).

Replays the live paper-trader rules over historical daily data:
  - $100 paper contribution at the start of each two-week period ($50 start).
  - Up to two $50 buys per two-week period, and ONLY when the coin's price
    is below its 100-week (700-day) moving average. No buys in a period
    where no coin is below its MA. If EVERY coin is below its MA in a
    period, the $100 is split equally across all of them (equal USD per
    coin). Subset case: the two $50 buys go to the qualifying coins deepest
    below their MA (largest % discount).
    No buys until the coin has 700 days of history.
  - Sell a coin's entire position only when its price reaches 30% above that
    coin's average buy price (its DCA).
  - No leverage, no fees, cash earns nothing. Paper only: no real orders.

Also computes a buy-and-hold baseline that receives the same contributions
(split equally across coins) for comparison.

--weekday-analysis: run the strategy seven times, allowing buys only on one
weekday each time, and report which weekday produced the best return. Writes
bot/weekday_analysis.json.

Data: Yahoo Finance daily closes (free, no key) for every window, since the
strategy needs 700 days of warmup history that CoinGecko's free tier cannot
serve. Pass --prices-file to replay a saved series instead.

Usage:
    python bot/backtest.py [--days 730] [--prices-file prices.json]
                           [--out backtest_results.json] [--weekday-analysis]
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
MAX_BUYS_PER_PERIOD = int(os.getenv("MAX_BUYS_PER_PERIOD", "2"))
MA_DAYS = int(os.getenv("MA_DAYS", "700"))          # 100-week moving average
TAKE_PROFIT_MULT = float(os.getenv("TAKE_PROFIT_MULT", "1.30"))
PERIOD_CONTRIB = float(os.getenv("PERIOD_CONTRIB", "100"))


def period_key(day):
    """Two-week period key for a 'YYYY-MM-DD' date: ISO year +
    floor((ISO week - 1) / 2), exactly 26 per 52-week year."""
    dt = datetime.strptime(day, "%Y-%m-%d")
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-P{(iso_week - 1) // 2:02d}"


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


def run_backtest(full_series, days, buy_weekday=None):
    """Replay the strategy. If buy_weekday is set (0=Monday), buys are only
    executed on that weekday (sells still run every day); used by the
    weekday analysis."""
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
    pos = {c: {"units": 0.0, "invested": 0.0} for c in COINS}
    trades = []
    equity_curve = []
    cur_period, buys_this_period, bought_this_period = None, 0, set()

    for day in grid:
        period = period_key(day)
        if period != cur_period:
            # 1) biweekly contribution at the start of each two-week period
            cash += PERIOD_CONTRIB
            contributed += PERIOD_CONTRIB
            cur_period = period
            buys_this_period = 0
            bought_this_period = set()
        day_dt = datetime.strptime(day, "%Y-%m-%d")
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
                               "weekday": day_dt.strftime("%A"),
                               "price": round(price, 4),
                               "dca": round(dca, 4),
                               "proceeds": round(proceeds, 2),
                               "pnl": round(pnl, 2)})
                pos[c] = {"units": 0.0, "invested": 0.0}
        # 3) buys: only below the 100-week MA; nothing if no coin is below
        #    its MA. If EVERY coin is below its MA, split the period's $100
        #    equally across all of them. Otherwise the two $50 buys go to
        #    the qualifying coins deepest below their MA (largest % discount).
        if buy_weekday is None or day_dt.weekday() == buy_weekday:
            qualifiers = [c for c in COINS
                          if (price_at(c, day) is not None
                              and ma_at(c, day) is not None
                              and price_at(c, day) < ma_at(c, day)
                              and c not in bought_this_period)]
            all_below = len(qualifiers) == len(COINS)
            if all_below:
                spend = min(cash, PERIOD_CONTRIB)
                total_cents = int(round(spend * 100))
                base, extra = divmod(total_cents, len(qualifiers))
                for i, c in enumerate(qualifiers):
                    amount = (base + (1 if i < extra else 0)) / 100.0
                    price = price_at(c, day)
                    units = amount / price
                    p = pos[c]
                    p["units"] += units
                    p["invested"] += amount
                    bought_this_period.add(c)
                    cash -= amount
                    trades.append({"coin": c, "side": "buy", "date": day,
                                   "weekday": day_dt.strftime("%A"),
                                   "price": round(price, 4),
                                   "amount": round(amount, 2),
                                   "dca": round(p["invested"] / p["units"], 4)})
                buys_this_period = len(qualifiers)
            else:
                # Subset case: rank qualifiers by depth below the MA
                # (deepest discount first); the buys go to the top-ranked.
                ranked = sorted(
                    qualifiers,
                    key=lambda c: price_at(c, day) / ma_at(c, day))
                for c in ranked:
                    if buys_this_period >= MAX_BUYS_PER_PERIOD:
                        break
                    price = price_at(c, day)
                    if price is None:
                        continue
                    p = pos[c]
                    if cash >= BUY_USD:
                        units = BUY_USD / price
                        p["units"] += units
                        p["invested"] += BUY_USD
                        buys_this_period += 1
                        bought_this_period.add(c)
                        cash -= BUY_USD
                        trades.append({"coin": c, "side": "buy", "date": day,
                                       "weekday": day_dt.strftime("%A"),
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


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


def weekday_analysis(full_series, days, out="bot/weekday_analysis.json"):
    """Run the strategy seven times, allowing buys on only one weekday each
    time, and report which weekday produced the best return."""
    per_weekday = {}
    for w, name in enumerate(WEEKDAYS):
        trades, equity, contributed = run_backtest(
            full_series, days, buy_weekday=w)
        end_balance = equity[-1][1]
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        per_weekday[name] = {
            "ending_equity": end_balance,
            "total_contributed": round(contributed, 2),
            "return_pct": round((end_balance / contributed - 1) * 100, 2),
            "buys": len(buys),
            "sells": len(sells),
            "max_drawdown_pct": max_drawdown(equity),
        }
    best = max(per_weekday.items(), key=lambda kv: kv[1]["return_pct"])[0]
    end_dt = max(datetime.strptime(d, "%Y-%m-%d")
                 for c in COINS for d, _ in full_series[c])
    start = (end_dt - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": (f"DCA value: up to {MAX_BUYS_PER_PERIOD}x ${BUY_USD:.0f} buys "
                     f"per two-week period, only below the {MA_DAYS}-day MA, "
                     f"${PERIOD_CONTRIB:.0f} contributed per period, "
                     f"sell full position at +{round((TAKE_PROFIT_MULT - 1) * 100)}% over coin DCA"),
        "window": {"start": start, "end": end_dt.strftime("%Y-%m-%d"),
                   "days": days},
        "per_weekday": per_weekday,
        "best_weekday": best,
        "note": ("Buys restricted to one weekday per simulation run; sells "
                 "ran every day in all runs. Simulated past performance does "
                 "not predict future results. Educational only, not financial "
                 "advice."),
    }
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    return result


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
    ap.add_argument("--weekday-analysis", action="store_true",
                    help="run the strategy once per weekday (buys only on that "
                         "weekday) and write bot/weekday_analysis.json")
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

    if args.weekday_analysis:
        result = weekday_analysis(full, args.days)
        print(f"Window: {result['window']['start']} -> {result['window']['end']}")
        print(f"Strategy: {result['strategy']}")
        print("\nWeekday buy analysis (buys only on that weekday):")
        for name in WEEKDAYS:
            r = result["per_weekday"][name]
            mark = "  <-- best" if name == result["best_weekday"] else ""
            print(f"  {name:9s}: equity ${r['ending_equity']:>10,.2f} "
                  f"({r['return_pct']:+6.1f}%), buys {r['buys']:>3d}, "
                  f"sells {r['sells']:>2d}, max DD {r['max_drawdown_pct']:.1f}%{mark}")
        print("Wrote bot/weekday_analysis.json")
        return 0

    trades, equity, contributed = run_backtest(full, args.days)

    end_dt = max(datetime.strptime(d, "%Y-%m-%d")
                 for c in COINS for d, _ in full[c])
    grid = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(args.days - 1, -1, -1)]
    # biweekly contribution schedule: first day of each two-week period
    seen, contrib_schedule = set(), []
    for d in grid:
        pk = period_key(d)
        if pk not in seen:
            seen.add(pk)
            contrib_schedule.append((d, PERIOD_CONTRIB))
    base_total, base_per = baseline(full, args.days, contrib_schedule)

    months = monthly_returns(equity)
    end_balance = equity[-1][1]
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    per_coin_pnl = {c: round(sum(t.get("pnl", 0) for t in sells
                                 if t["coin"] == c), 2) for c in COINS}

    result = {
        "strategy": (f"DCA value: up to {MAX_BUYS_PER_PERIOD}x ${BUY_USD:.0f} buys "
                     f"per two-week period, only below the {MA_DAYS}-day moving average "
                     f"(no buys if no coin qualifies), "
                     f"${PERIOD_CONTRIB:.0f} contributed per two-week period, "
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
