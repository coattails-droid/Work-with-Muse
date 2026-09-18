#!/usr/bin/env python3
"""Strategy backtest: "buy the dips".

Replays a dip-buying strategy over historical daily prices, paper only.

Rules (exactly as offered):
  - Entry: a coin drops 5%+ in a single day -> buy at that day's close.
  - Position size: 20% of available cash per signal, one open position
    per coin at a time, minimum $1 per trade.
  - Exit: sell when the price rebounds 10% above the entry close,
    or after 14 days, whichever comes first.
  - No leverage, no fees, cash earns nothing. Paper only: no real orders.

Also computes an equal-split buy-and-hold baseline ($10 per coin) over the
same window for comparison.

Usage:
    python bot/backtest.py [--days 180] [--prices-file prices.json]
                           [--out backtest_results.json]
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

COINS = ["bitcoin", "ethereum", "ripple", "bitcoin-cash", "kaspa"]
STARTING_CASH = 50.0

DIP_PCT = float(os.getenv("DIP_PCT", "5"))                 # entry: daily drop %
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "10"))  # exit: rebound %
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "14"))       # exit: time stop
POSITION_PCT = float(os.getenv("POSITION_PCT", "20"))       # cash per trade %


def fetch_history(days):
    series = {}
    for i, cid in enumerate(COINS):
        url = (f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
               f"?vs_currency=usd&days={days}")
        req = urllib.request.Request(url, headers={"User-Agent": "muse-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        pts = data["prices"]
        series[cid] = [
            (datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d"), p)
            for ts, p in pts
        ]
        if i < len(COINS) - 1:
            time.sleep(12)
    return series


def run_backtest(series):
    dates = [d for d, _ in series[COINS[0]]]
    closes = {c: [p for _, p in series[c]] for c in COINS}

    cash = STARTING_CASH
    open_pos = {c: None for c in COINS}  # one position per coin
    trades = []
    equity_curve = []

    for i, day in enumerate(dates):
        # 1) exits
        for c in COINS:
            pos = open_pos[c]
            if not pos:
                continue
            price = closes[c][i]
            entry_price = pos["entry_price"]
            held = (datetime.strptime(day, "%Y-%m-%d")
                    - datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days
            if price >= entry_price * (1 + TAKE_PROFIT_PCT / 100):
                reason = "take-profit"
            elif held >= MAX_HOLD_DAYS:
                reason = "time-stop"
            else:
                continue
            proceeds = pos["units"] * price
            pnl = proceeds - pos["invested"]
            cash += proceeds
            trades.append({**pos, "exit_date": day, "exit_price": price,
                           "proceeds": round(proceeds, 2),
                           "pnl": round(pnl, 2), "reason": reason})
            open_pos[c] = None
        # 2) entries
        for c in COINS:
            if i == 0 or open_pos[c]:
                continue
            prev, price = closes[c][i - 1], closes[c][i]
            if prev <= 0:
                continue
            if (price / prev - 1) * 100 <= -DIP_PCT:
                invest = cash * (POSITION_PCT / 100)
                if invest >= 1.0:
                    units = invest / price
                    cash -= invest
                    open_pos[c] = {"coin": c, "entry_date": day,
                                   "entry_price": price,
                                   "invested": round(invest, 2),
                                   "units": units}
        # 3) equity
        equity = cash + sum(
            (open_pos[c]["units"] * closes[c][i]) for c in COINS if open_pos[c])
        equity_curve.append((day, round(equity, 2)))

    # force-close anything still open at the end
    for c in COINS:
        pos = open_pos[c]
        if pos:
            price = closes[c][-1]
            proceeds = pos["units"] * price
            pnl = proceeds - pos["invested"]
            cash += proceeds
            trades.append({**pos, "exit_date": dates[-1], "exit_price": price,
                           "proceeds": round(proceeds, 2),
                           "pnl": round(pnl, 2), "reason": "window-end"})
            open_pos[c] = None
    equity_curve[-1] = (dates[-1], round(cash, 2))
    return trades, equity_curve


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


def baseline(series):
    """Equal-split buy-and-hold: $10 per coin at first close."""
    per_coin = STARTING_CASH / len(COINS)
    total = 0.0
    per = {}
    for c in COINS:
        first, last = series[c][0][1], series[c][-1][1]
        val = per_coin / first * last
        per[c] = round(val, 2)
        total += val
    return round(total, 2), per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--prices-file", default=None)
    ap.add_argument("--out", default="backtest_results.json")
    args = ap.parse_args()

    if args.prices_file:
        with open(args.prices_file) as f:
            raw = json.load(f)
        series = {c: [(p["date"], p["price"]) for p in raw["coins"][c]]
                  for c in COINS}
        source = f"local file {args.prices_file} (CoinGecko)"
    else:
        series = fetch_history(args.days)
        source = "CoinGecko market_chart, daily, vs USD"

    # sanity: all series same length
    n = len(series[COINS[0]])
    assert all(len(series[c]) == n for c in COINS), "ragged price series"

    trades, equity = run_backtest(series)
    base_total, base_per = baseline(series)
    months = monthly_returns(equity)

    end_balance = equity[-1][1]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    per_coin_pnl = {c: round(sum(t["pnl"] for t in trades if t["coin"] == c), 2)
                    for c in COINS}

    result = {
        "strategy": (f"buy {DIP_PCT}%+ daily dips with {POSITION_PCT}% of cash, "
                     f"sell at +{TAKE_PROFIT_PCT}% rebound or after "
                     f"{MAX_HOLD_DAYS} days"),
        "source": source,
        "window": {"start": equity[0][0], "end": equity[-1][0],
                   "days": n},
        "starting_cash": STARTING_CASH,
        "ending_balance": end_balance,
        "total_return_pct": round((end_balance / STARTING_CASH - 1) * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "per_coin_pnl": per_coin_pnl,
        "best_month": max(months.items(), key=lambda kv: kv[1]) if months else None,
        "worst_month": min(months.items(), key=lambda kv: kv[1]) if months else None,
        "max_drawdown_pct": max_drawdown(equity),
        "baseline_buy_and_hold": base_total,
        "baseline_per_coin": base_per,
        "baseline_return_pct": round((base_total / STARTING_CASH - 1) * 100, 2),
        "disclaimer": ("Simulated past performance does not predict future "
                       "results. Educational only, not financial advice."),
    }

    with open(args.out, "w") as f:
        json.dump({**result, "trade_ledger": trades}, f, indent=2)

    print(f"Window: {result['window']['start']} -> {result['window']['end']} "
          f"({n} days)")
    print(f"Strategy: {result['strategy']}")
    print(f"Starting cash: ${STARTING_CASH:.2f}")
    print(f"Ending balance: ${end_balance:.2f} "
          f"({result['total_return_pct']:+.1f}%)")
    print(f"Trades: {len(trades)}, win rate: {result['win_rate_pct']}%")
    print(f"Max drawdown: {result['max_drawdown_pct']}%")
    print(f"Buy-and-hold baseline: ${base_total:.2f} "
          f"({result['baseline_return_pct']:+.1f}%)")
    print(f"Results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
