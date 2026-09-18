#!/usr/bin/env python3
"""Fetch the latest paper-trade log from GitHub and email a daily briefing via Gmail.

Paper only. Exits non-zero with a clear message when Gmail is not connected.
"""
import base64
import json
import subprocess
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_response_body

CRED = "custom.github"
ALLOWED = ["api.github.com", "github.com"]
OWNER, REPO = "coattails-droid", "Work-with-Muse"
TO = "foughtct@gmail.com"
# Secondary Gmail account the briefing sends from (user keeps personal Gmail private)
GMAIL_ACCOUNT = "607ad66901584abb870c48950f8cedf2"

NAMES = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ripple": "XRP",
    "bitcoin-cash": "BCH",
    "kaspa": "KAS",
}


def gh_get(path):
    req = urllib.request.Request("https://api.github.com" + path, method="GET")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "muse-crypto-briefing/1.0")
    add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(read_response_body(r).decode())


def gmail_connected():
    try:
        out = subprocess.run(
            ["hatch_gws_cli", "gmail", "status"],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(out.stdout).get("status") == "connected"
    except Exception:
        return False


def fmt_price(p):
    return f"${p:,.2f}" if p >= 1 else f"${p:,.6f}"


def main():
    if not gmail_connected():
        print(
            "GMAIL_NOT_CONNECTED: connect Gmail before the briefing can be sent.",
            file=sys.stderr,
        )
        return 2

    data = gh_get(f"/repos/{OWNER}/{REPO}/contents/bot/paper_state.json?ref=main")
    state = json.loads(base64.b64decode(data["content"]).decode())
    hist = state.get("trade_history", [])
    if not hist:
        print("No trade history in paper_state.json yet.", file=sys.stderr)
        return 1

    latest = hist[-1]
    ref = hist[-7] if len(hist) >= 7 else hist[0]  # ~24h back at 4h cadence

    lines = [
        "Crypto paper-trading briefing (paper only — no real money moves)",
        f"Paper balance: ${state.get('balance', 0):.2f} (started at $50.00)",
        "",
        "Latest prices:",
    ]
    for cid, price in latest["prices"].items():
        sym = NAMES.get(cid, cid.upper())
        old = ref["prices"].get(cid)
        chg = f" ({(price - old) / old * 100:+.1f}% vs ~24h ago)" if old else ""
        lines.append(f"  {sym}: {fmt_price(price)}{chg}")
    lines += ["", "Latest signals:"]
    for s in latest.get("signals", []):
        lines.append(
            f"  {NAMES.get(s['symbol'], s['symbol'].upper())}: {s['side']} — {s['reason']}"
        )
    lines += [
        "",
        "Reminder: turning $50 into $1000 is a 20x return. This log is for",
        "learning and testing ideas, not a promise of returns.",
    ]
    body = "\n".join(lines)
    today = datetime.now().strftime("%b %d")
    subject = f"Crypto paper briefing — {today}"

    r = subprocess.run(
        ["hatch_gws_cli", "gmail", "+send", "--account", GMAIL_ACCOUNT,
         "--to", TO, "--subject", subject, "--body", body],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        print(r.stdout[-2000:], file=sys.stderr)
        print(r.stderr[-2000:], file=sys.stderr)
        return 1
    print(f"Briefing sent to {TO}")


if __name__ == "__main__":
    sys.exit(main())
