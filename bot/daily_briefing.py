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
    "pyth-network": "PYTH",
    "near": "NEAR",
    "bittensor": "TAO",
    "tao-bot": "TAOBOT",
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


def fetch_digest_headlines(folder="digests"):
    """Return (day, headlines) from the latest news digest in the repo.

    Headlines are plain-text (title, source) tuples, max 8. Returns
    (None, []) when no digest is available; the briefing sends without it.
    """
    import re
    from datetime import timedelta, timezone

    for delta in (0, 1):
        day = (datetime.now(timezone.utc) - timedelta(days=delta)).strftime("%Y-%m-%d")
        try:
            data = gh_get(f"/repos/{OWNER}/{REPO}/contents/{folder}/{day}.md?ref=main")
        except Exception:
            continue
        text = base64.b64decode(data["content"]).decode()
        heads = []
        for line in text.splitlines():
            m = re.match(r"- \*\*\[(.*?)\]\(.*?\)\*\*\s*[—–-]\s*(.*)", line)
            if m and len(heads) < 8:
                heads.append((m.group(1).strip(), m.group(2).strip()))
        if heads:
            return day, heads
    return None, []


def fetch_research_briefs(max_reports=2):
    """Return [(day, topic, findings)] for the most recent research briefs.

    Looks at research/ for briefs dated today or yesterday (UTC, matching
    the research agent's dating). Findings are up to 4 key-finding bullets
    with source refs stripped. Returns [] when nothing recent exists.
    """
    import re
    from datetime import timedelta, timezone

    try:
        items = gh_get(f"/repos/{OWNER}/{REPO}/contents/research?ref=main")
    except Exception:
        return []
    days = {(datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
            for d in (0, 1)}
    candidates = []
    for it in items:
        name = it.get("name", "")
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(.+)\.md$", name)
        if m and m.group(1) in days and name != "INDEX.md":
            candidates.append((m.group(1), it["path"]))
    candidates.sort(reverse=True)
    out = []
    for day, path in candidates[:max_reports]:
        try:
            data = gh_get(f"/repos/{OWNER}/{REPO}/contents/{path}?ref=main")
        except Exception:
            continue
        text = base64.b64decode(data["content"]).decode()
        title = re.search(r"^# Research: (.*)$", text, re.M)
        findings = []
        in_kf = False
        for line in text.splitlines():
            if line.startswith("## Key findings"):
                in_kf = True
                continue
            if in_kf:
                if line.startswith("## "):
                    break
                if line.startswith("- ") and len(findings) < 4:
                    findings.append(
                        re.sub(r"\s*\[\d+\]$", "", line[2:].strip()))
        out.append((day, title.group(1).strip() if title else path, findings))
    return out


def fetch_model_news(max_models=6):
    """Return (day, models) from the latest AI digest's open-models section.

    Models are (name, url, details) tuples drawn from the digest's
    "New open models for local hardware" section. Returns (None, []) when
    no recent digest has that section; the briefing sends without it.
    """
    import re
    from datetime import timedelta, timezone

    for delta in (0, 1):
        day = (datetime.now(timezone.utc) - timedelta(days=delta)).strftime("%Y-%m-%d")
        try:
            data = gh_get(f"/repos/{OWNER}/{REPO}/contents/digests-ai/{day}.md?ref=main")
        except Exception:
            continue
        text = base64.b64decode(data["content"]).decode()
        m = re.search(r"## New open models for local hardware\n(.*?)(?:\n## |\Z)",
                      text, re.S)
        if not m:
            continue
        models = []
        for line in m.group(1).splitlines():
            mm = re.match(r"- \*\*\[(.*?)\]\((.*?)\)\*\*\s*[—–-]\s*(.*)", line)
            if mm and len(models) < max_models:
                models.append((mm.group(1).strip(), mm.group(2).strip(),
                               mm.group(3).strip()))
        if models:
            return day, models
    return None, []


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
        "Reminder: turning $50 into $500 is a 10x return. This log is for",
        "learning and testing ideas, not a promise of returns.",
    ]

    digest_day, headlines = fetch_digest_headlines("digests")
    if headlines:
        lines += ["", f"Today's crypto headlines ({digest_day}):"]
        for title, source in headlines:
            lines.append(f"  - {title} [{source}]")
        lines.append("  Full digest with links: digests/ folder in the repo.")

    ai_day, ai_headlines = fetch_digest_headlines("digests-ai")
    if ai_headlines:
        lines += ["", f"Today's AI headlines ({ai_day}):"]
        for title, source in ai_headlines:
            lines.append(f"  - {title} [{source}]")
        lines.append("  Full digest with links: digests-ai/ folder in the repo.")

    model_day, models = fetch_model_news()
    if models:
        lines += ["", f"New open models for local hardware ({model_day}):"]
        for name, url, details in models:
            lines.append(f"  - {name} ({details})")
        lines.append("  Full list with Hugging Face links: digests-ai/ folder in the repo.")

    briefs = fetch_research_briefs()
    if briefs:
        lines += ["", "New research briefs:"]
        for day, topic, findings in briefs:
            lines.append(f"  {topic} ({day}):")
            for f in findings:
                lines.append(f"    - {f}")
        lines.append("  Full briefs with sources: research/ folder in the repo.")

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
