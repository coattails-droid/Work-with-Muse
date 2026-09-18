"""
Paper trading bot - educational example.
Does NOT place real orders. Fetches public prices and simulates a strategy
with strict risk controls.

Goal context: turning $50 into $1000 is a 20x return. That requires extreme
risk and in practice usually results in losing the entire $50. This bot
defaults to paper trading so you can test ideas without risking money.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Config
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "50"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "2"))  # risk 2% per trade
SYMBOLS = os.getenv("SYMBOLS", "bitcoin,ethereum,solana").split(",")
STATE_FILE = os.getenv("STATE_FILE", "bot/paper_state.json")

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"


def fetch_prices():
    ids = ",".join(s.strip() for s in SYMBOLS if s.strip())
    url = COINGECKO_URL.format(ids=ids)
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    # normalize to {symbol: price}
    return {k: v["usd"] for k, v in data.items() if "usd" in v}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "balance": STARTING_BALANCE,
        "positions": {},
        "trade_history": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def simple_momentum_signal(prices, state):
    """
    Very simple example signal: placeholder for your own logic.
    In a real workflow you'd compute indicators from historical candles.
    Here we just log prices and do NOT auto-trade aggressively.

    Returns list of (symbol, side, reason) suggestions.
    """
    suggestions = []
    # Example: if we have no position, suggest watching, not trading.
    # Replace this with your tested strategy.
    for sym, price in prices.items():
        suggestions.append((sym, "hold", f"price=${price:,.2f} - no auto-trade in safe mode"))
    return suggestions


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting paper trading run")
    print(f"Starting balance: ${STARTING_BALANCE:.2f}, max risk per trade: {MAX_RISK_PER_TRADE_PCT}%")
    print("MODE: PAPER TRADING ONLY - no real orders placed")

    try:
        prices = fetch_prices()
    except Exception as e:
        print(f"Failed to fetch prices: {e}", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    signals = simple_momentum_signal(prices, state)

    print("\nPrices:")
    for sym, price in prices.items():
        print(f"  {sym}: ${price:,.2f}")

    print("\nSignals (paper only):")
    for sym, side, reason in signals:
        print(f"  {sym}: {side} - {reason}")

    # Risk reminder
    risk_per_trade_dollars = state["balance"] * (MAX_RISK_PER_TRADE_PCT / 100)
    print(f"\nRisk controls:")
    print(f"  Balance: ${state['balance']:.2f}")
    print(f"  Max risk per trade: ${risk_per_trade_dollars:.2f} ({MAX_RISK_PER_TRADE_PCT}%)")
    print(f"  To turn $50 into $1000 you need 20x. That implies risking ruin.")
    print(f"  This bot will NOT use leverage by default.")

    # Append a log entry
    log_entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "prices": prices,
        "balance": state["balance"],
        "signals": [{"symbol": s, "side": sd, "reason": r} for s, sd, r in signals],
    }
    state["trade_history"].append(log_entry)
    # keep history bounded
    state["trade_history"] = state["trade_history"][-200:]
    save_state(state)
    print(f"\nState saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
