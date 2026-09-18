# Crypto Paper Trading Agentic Workflow

This is a starter for an agentic crypto workflow on GitHub. It runs on a schedule,
fetches public prices, and logs paper-trade signals. No real orders are placed.

## ⚠️ Honest risk note

You mentioned turning $50 into $1000. That's a 20x return.

- To get 20x quickly you need extreme leverage or extremely lucky concentrated bets.
- In practice, that almost always means losing the entire $50.
- There is no workflow, bot, or AI agent that can reliably turn $50 into $1000 by "betting on price changes."
- If anyone promises that, it's a scam.

This repo is set up for **paper trading first**: test strategies with fake money,
track performance, and learn risk management before you consider risking anything real.
If you do trade real money, never trade money you can't afford to lose.

## What's included

- `bot/paper_trader.py` - paper trading bot (stdlib only, no API keys needed)
  - Fetches prices from CoinGecko public API
  - Logs signals and balance to `bot/paper_state.json`
  - Enforces max risk per trade (default 2%)
  - Safe mode: does NOT auto-trade aggressively

- `.github/workflows/crypto-agent.yml` - GitHub Actions workflow
  - Runs every 4 hours + manual trigger
  - Commits paper state back to repo
  - Uploads log as artifact

- `bot/failure_detective.py` + `.github/workflows/failure-detective.yml` - failure detective
  - Watches `crypto-paper-agent`; when a run fails, it reads the failed logs,
    matches the error against known failure patterns, and opens a GitHub issue
    with the likely cause and a suggested fix
  - Dedupes: won't open a repeat issue for the same error signature
  - Read-only by design: never retries runs or changes code on its own
  - Also watches `price-sentinel`

- `bot/price_sentinel.py` + `.github/workflows/price-sentinel.yml` - price sentinel
  - Runs hourly; checks 24h moves for BTC, ETH, XRP, BCH, KAS via CoinGecko
  - Opens a GitHub issue when a coin moves >= 5% (configurable) in 24h,
    comments fresh readings on the open alert, and closes it automatically
    once the move settles
  - Manual trigger supports a custom threshold and a dry-run mode

- `bot/backtest.py` + `.github/workflows/backtest.yml` - strategy backtest
  - Manually-triggered replay of the "buy the dips" paper strategy over
    historical daily prices (no real orders possible)
  - Compares against an equal-split buy-and-hold baseline; uploads a
    results file as a run artifact

- `bot/news_digest.py` + `.github/workflows/news-digest.yml` - daily news digest
  - Every morning pulls crypto RSS feeds (CoinDesk, CoinTelegraph, Decrypt),
    keeps the last day's stories, and commits `digests/YYYY-MM-DD.md`
  - The morning email briefing includes the day's headlines automatically

- `bot/news_digest.py` + `.github/workflows/ai-digest.yml` - daily AI news digest
  - Same engine, different feeds: MIT Tech Review, TechCrunch AI, The Decoder
  - Runs 7:15am ET, commits `digests-ai/YYYY-MM-DD.md`; headlines also go
    out in the morning email briefing

## Quick start (local)

```bash
python3 bot/paper_trader.py
cat bot/paper_state.json
```

## Push to GitHub

1. Create a new repo on GitHub (empty)
2. Then:
```bash
cd crypto-trading-workflow
git init
git add .
git commit -m "init paper trading agent"
git branch -M main
git remote add origin git@github.com:YOUR_USER/YOUR_REPO.git
git push -u origin main
```

3. Go to Actions tab to see scheduled runs. Use "Run workflow" to trigger manually.

## How I can keep you updated

Once this is in GitHub, I can:
- Check workflow runs and summarize P&L / signals
- Alert you when runs fail
- Help you iterate on strategy logic (e.g., add RSI, momentum, stop-loss)
- Set up a daily briefing here in chat

Want me to wire that up? Tell me your repo name and I'll help push this and create a scheduled check-in.

## Next steps if you want real trading (not recommended for 20x goals)

- Use a reputable exchange's testnet / paper account first
- Never give your API secret keys to a workflow without IP allowlists and withdraw-disabled keys
- Start with spot, not leverage
- Keep position sizing small and use stop-losses

## Disclaimer

Educational only, not financial advice.
