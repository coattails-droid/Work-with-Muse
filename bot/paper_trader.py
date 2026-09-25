#!/usr/bin/env python3
"""Paper trading bot - DCA value strategy. Paper only: no real orders.

Strategy (set 2026-09-25):
  - $100 of paper cash is contributed on the first run of each calendar month
    (tracked in state, so it happens exactly once per month).
  - Buy $50 of a coin, at most twice per coin per calendar month, and only
    when the coin's price is below its 100-week (700-day) moving average.
  - Sell a coin's entire position only when its price is >= 30% above that
    coin's average buy price (its DCA).
  - No leverage. Cash earns nothing.

Live prices come from CoinGecko; the moving average uses Yahoo Finance
daily closes (free, no key) because it needs 700 days of history.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "50"))
BUY_USD = float(os.getenv("BUY_USD", "50"))
MAX_BUYS_PER_MONTH = int(os.getenv("MAX_BUYS_PER_MONTH", "2"))
MA_DAYS = int(os.getenv("MA_DAYS", "700"))
TAKE_PROFIT_MULT = float(os.getenv("TAKE_PROFIT_MULT", "1.30"))
MONTHLY_CONTRIB = float(os.getenv("MONTHLY_CONTRIB", "100"))
SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS",
    "bitcoin,ethereum,ripple,bitcoin-cash,kaspa,pyth-network,near,bittensor,tao-bot"
).split(",") if s.strip()]
STATE_FILE = os.getenv("STATE_FILE", "bot/paper_state.json")

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"

# CoinGecko id -> Yahoo Finance ticker (for 700-day moving averages)
YAHOO = {
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


def fetch_prices():
    ids = ",".join(SYMBOLS)
    url = COINGECKO_URL.format(ids=ids)
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return {k: v["usd"] for k, v in data.items() if "usd" in v}


def fetch_ma(sym):
    """MA_DAYS-day simple moving average of daily closes, or None if the
    coin lacks enough history (too young for a 100-week average)."""
    ticker = YAHOO.get(sym)
    if not ticker:
        return None
    try:
        to_s = int(time.time())
        from_s = to_s - (MA_DAYS + 10) * 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=1d&period1={from_s}&period2={to_s}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"]
                  if c is not None]
        if len(closes) < MA_DAYS:
            return None
        return sum(closes[-MA_DAYS:]) / MA_DAYS
    except Exception as e:
        print(f"  MA fetch failed for {sym}: {e}", file=sys.stderr)
        return None


def blank_position():
    return {"units": 0.0, "invested": 0.0, "buy_month": None, "buy_count": 0}


def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
    st.setdefault("balance", STARTING_BALANCE)
    st.setdefault("positions", {})
    st.setdefault("trade_history", [])
    st.setdefault("contrib_month", None)
    # old states predate contributions; the starting balance was the only funding
    st.setdefault("total_contributed", STARTING_BALANCE)
    if "started_at" not in st:
        st["started_at"] = datetime.now(timezone.utc).isoformat()
    return st


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    print(f"[{now.isoformat()}] Starting paper trading run (DCA value strategy)")
    print("MODE: PAPER TRADING ONLY - no real orders placed")

    try:
        prices = fetch_prices()
    except Exception as e:
        print(f"Failed to fetch prices: {e}", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    actions = []
    signals = []

    # 1) monthly contribution, exactly once per calendar month
    if state.get("contrib_month") != month:
        state["balance"] += MONTHLY_CONTRIB
        state["total_contributed"] += MONTHLY_CONTRIB
        state["contrib_month"] = month
        actions.append(f"contributed ${MONTHLY_CONTRIB:.2f} paper cash for {month}")
        print(f"Monthly contribution: +${MONTHLY_CONTRIB:.2f} (month {month})")

    # 2) moving averages (one Yahoo request per coin)
    mas = {}
    for i, sym in enumerate(SYMBOLS):
        mas[sym] = fetch_ma(sym)
        if i < len(SYMBOLS) - 1:
            time.sleep(2)

    # 3) sells first: full exit at +30% over the coin's DCA
    for sym in SYMBOLS:
        price = prices.get(sym)
        if price is None:
            continue
        pos = state["positions"].get(sym) or blank_position()
        if pos["units"] <= 0:
            continue
        dca = pos["invested"] / pos["units"]
        if price >= dca * TAKE_PROFIT_MULT:
            proceeds = pos["units"] * price
            pnl = proceeds - pos["invested"]
            state["balance"] += proceeds
            actions.append(
                f"SOLD {sym}: {pos['units']:.6f} @ ${price:,.2f} = ${proceeds:,.2f} "
                f"(DCA ${dca:,.2f}, pnl ${pnl:+,.2f})")
            signals.append((sym, "sell",
                            f"price ${price:,.2f} >= 30% over DCA ${dca:,.2f}"))
            state["positions"][sym] = {"units": 0.0, "invested": 0.0,
                                       "buy_month": pos.get("buy_month"),
                                       "buy_count": pos.get("buy_count", 0)}

    # 4) buys: $50, max 2 per coin per month, only below the 100-week MA
    for sym in SYMBOLS:
        price = prices.get(sym)
        if price is None:
            continue
        ma = mas.get(sym)
        pos = state["positions"].get(sym)
        if pos is None:
            pos = blank_position()
            state["positions"][sym] = pos
        if pos.get("buy_month") != month:
            pos["buy_month"] = month
            pos["buy_count"] = 0
        dca = pos["invested"] / pos["units"] if pos["units"] > 0 else None
        if ma is None:
            signals.append((sym, "hold",
                            f"price ${price:,.2f} - no 100-week MA yet (short history)"))
        elif price >= ma:
            signals.append((sym, "hold",
                            f"price ${price:,.2f} above 100-week MA ${ma:,.2f}"))
        elif pos["buy_count"] >= MAX_BUYS_PER_MONTH:
            signals.append((sym, "hold",
                            f"price ${price:,.2f} below MA but monthly buy cap reached"))
        elif state["balance"] < BUY_USD:
            signals.append((sym, "hold",
                            f"price ${price:,.2f} below MA but cash ${state['balance']:.2f} < ${BUY_USD:.0f}"))
        else:
            units = BUY_USD / price
            pos["units"] += units
            pos["invested"] += BUY_USD
            pos["buy_count"] += 1
            state["balance"] -= BUY_USD
            new_dca = pos["invested"] / pos["units"]
            actions.append(
                f"BOUGHT {sym}: {units:.6f} @ ${price:,.2f} = ${BUY_USD:.2f} "
                f"(below 100-week MA ${ma:,.2f}; DCA now ${new_dca:,.2f})")
            signals.append((sym, "buy",
                            f"${BUY_USD:.0f} below 100-week MA ${ma:,.2f}"))

    print("\nPrices vs 100-week MA:")
    for sym in SYMBOLS:
        price = prices.get(sym)
        ma = mas.get(sym)
        if price is None:
            print(f"  {sym}: no price")
        elif ma is None:
            print(f"  {sym}: ${price:,.2f}  (MA unavailable)")
        else:
            tag = "BELOW" if price < ma else "above"
            print(f"  {sym}: ${price:,.2f} vs MA ${ma:,.2f} [{tag}]")

    if actions:
        print("\nActions:")
        for a in actions:
            print(f"  {a}")
    else:
        print("\nNo trades this run.")

    open_value = sum(
        (state["positions"].get(s) or blank_position())["units"] * prices[s]
        for s in SYMBOLS
        if s in prices and (state["positions"].get(s) or {}).get("units"))
    equity = state["balance"] + open_value
    print(f"\nCash: ${state['balance']:,.2f} | Open positions: ${open_value:,.2f} | "
          f"Equity: ${equity:,.2f} | Contributed: ${state['total_contributed']:,.2f}")

    log_entry = {
        "at": now.isoformat(),
        "prices": prices,
        "balance": round(state["balance"], 2),
        "total_contributed": round(state["total_contributed"], 2),
        "equity": round(equity, 2),
        "actions": actions,
        "signals": [{"symbol": s, "side": sd, "reason": r}
                    for s, sd, r in signals],
    }
    state["trade_history"].append(log_entry)
    state["trade_history"] = state["trade_history"][-200:]
    save_state(state)
    print(f"State saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
