#!/usr/bin/env python3
"""Build the mission-control dashboard (docs/index.html).

Run by the `dashboard` workflow (hourly + manual). Reads the checked-out
repo for paper-trade state, digests, and research briefs; queries the GitHub
API for workflow run health and open issues; writes one self-contained
static page (inline CSS, inline SVG chart, no JavaScript, no external
assets) to docs/ for GitHub Pages.

Env:
    GITHUB_TOKEN - token with actions:read and issues:read
    REPO         - "owner/repo"
    DRY_RUN=1    - print the HTML to stdout instead of writing docs/index.html
"""

import html as htmlmod
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://api.github.com"
ET = ZoneInfo("America/New_York")
NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "ripple": "XRP",
         "bitcoin-cash": "BCH", "kaspa": "KAS", "solana": "SOL",
         "pyth-network": "PYTH", "near": "NEAR", "bittensor": "TAO",
         "tao-bot": "TAOBOT"}

WORKFLOWS = ["crypto-paper-agent", "price-sentinel", "news-digest",
             "ai-digest", "backtest", "failure-detective",
             "research-agent", "dashboard"]

HEADLINE_RE = re.compile(r"- \*\*\[(.*?)\]\((.*?)\)\*\*")


def api(path, token):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "dashboard-builder"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def esc(s):
    return htmlmod.escape(str(s), quote=True)


def fmt_price(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    return f"${p:,.2f}" if p >= 1 else f"${p:,.6f}"


def load_state():
    try:
        with open("bot/paper_state.json") as f:
            return json.load(f)
    except Exception:
        return {}


def digest_headlines(folder, max_n=6):
    """Latest headlines (title, url) from today's or yesterday's digest."""
    for delta in (0, 1):
        day = (datetime.now(timezone.utc) - timedelta(days=delta)
               ).strftime("%Y-%m-%d")
        path = os.path.join(folder, f"{day}.md")
        if not os.path.exists(path):
            continue
        heads = []
        with open(path) as f:
            for line in f:
                m = HEADLINE_RE.match(line)
                if m and len(heads) < max_n:
                    heads.append((m.group(1).strip(), m.group(2).strip()))
        if heads:
            return day, heads
    return None, []


def research_list(max_n=6):
    """Parse research/INDEX.md -> [(day, topic, filename)]."""
    path = os.path.join("research", "INDEX.md")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            m = re.match(r"\| (\d{4}-\d{2}-\d{2}) \| (.*?) \| "
                         r"\[(.*?)\]\(.*?\) \|", line)
            if m:
                rows.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    rows.sort(reverse=True)
    return rows[:max_n]


def run_health(repo, token):
    """Latest run per workflow name -> {name: (status, conclusion, at, url)}."""
    try:
        data = api(f"/repos/{repo}/actions/runs?per_page=60", token)
    except Exception as e:
        print(f"run health fetch failed: {e}", file=sys.stderr)
        return {}
    latest = {}
    for run in data.get("workflow_runs", []):
        name = run.get("name")
        if name not in latest:
            latest[name] = (run.get("status"), run.get("conclusion"),
                            run.get("created_at"), run.get("html_url"))
    return latest


def open_issues(repo, token, max_n=10):
    try:
        items = api(f"/repos/{repo}/issues?state=open&per_page=30&sort=created"
                    f"&direction=desc", token)
    except Exception as e:
        print(f"issues fetch failed: {e}", file=sys.stderr)
        return []
    out = []
    for it in items:
        if "pull_request" in it:
            continue
        out.append({"title": it.get("title", ""),
                    "url": it.get("html_url", ""),
                    "labels": [l["name"] for l in it.get("labels", [])],
                    "created": (it.get("created_at", "") or "")[:10],
                    "number": it.get("number")})
        if len(out) >= max_n:
            break
    return out


def balance_chart(history, baseline):
    """Inline SVG balance-over-time chart."""
    bals = [(h.get("at", "")[:16].replace("T", " "),
             float(h.get("balance", 0))) for h in history
             if h.get("balance") is not None]
    if len(bals) < 2:
        return '<p class="muted">Not enough history yet — the chart appears after a few runs.</p>'
    W, H, P = 660, 220, 34
    vals = [b for _, b in bals]
    lo, hi = min(vals + [baseline]), max(vals + [baseline])
    span = (hi - lo) or 1.0
    def x(i): return P + i * (W - 2 * P) / (len(bals) - 1)
    def y(v): return H - P - (v - lo) / span * (H - 2 * P)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(bals))
    base_y = y(baseline)
    step = max(1, len(bals) // 6)
    labels = "".join(
        f'<text x="{x(i):.1f}" y="{H - 8}" class="axis">{esc(t[5:])}</text>'
        for i, (t, _) in enumerate(bals) if i % step == 0)
    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
        f'aria-label="Paper balance over time">'
        f'<line x1="{P}" y1="{base_y:.1f}" x2="{W - P}" y2="{base_y:.1f}" '
        f'class="baseline"/>'
        f'<text x="{W - P}" y="{base_y - 6:.1f}" class="axis end">$50 start</text>'
        f'<polyline points="{pts}" class="line"/>'
        f'<text x="{P}" y="18" class="axis">${hi:,.2f}</text>'
        f'<text x="{P}" y="{H - P + 16:.1f}" class="axis">${lo:,.2f}</text>'
        f'{labels}</svg>')


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:1024px;margin:0 auto;padding:24px 16px 64px}
header.top{display:flex;justify-content:space-between;align-items:baseline;
 flex-wrap:wrap;gap:8px;margin-bottom:20px}
h1{font-size:1.6rem;margin:0}
.muted{color:#8b949e;font-size:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.card .k{font-size:.75rem;color:#8b949e;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:1.5rem;font-weight:600;margin-top:4px}
.up{color:#3fb950}.down{color:#f85149}
section{margin-bottom:24px}
h2{font-size:1.05rem;margin:0 0 10px;color:#e6edf3}
table{width:100%;border-collapse:collapse;background:#161b22;
 border:1px solid #30363d;border-radius:8px;overflow:hidden;font-size:.9rem}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:600;font-size:.78rem;text-transform:uppercase}
tr:last-child td{border-bottom:none}
a{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.ok{background:#3fb950}.bad{background:#f85149}.run{background:#d29922}.idle{background:#6e7681}
ul.clean{list-style:none;margin:0;padding:0}
ul.clean li{padding:7px 12px;border-bottom:1px solid #21262d;background:#161b22;font-size:.9rem}
ul.clean li:first-child{border-radius:8px 8px 0 0}
ul.clean li:last-child{border-bottom:none;border-radius:0 0 8px 8px}
ul.clean{border:1px solid #30363d;border-radius:8px;overflow:hidden}
.tag{display:inline-block;background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb66;
 border-radius:10px;padding:0 8px;font-size:.72rem;margin-left:8px}
.chart{width:100%;height:auto;background:#161b22;border:1px solid #30363d;border-radius:8px}
.chart .line{fill:none;stroke:#58a6ff;stroke-width:2}
.chart .baseline{stroke:#6e7681;stroke-dasharray:5 4;stroke-width:1}
.chart .axis{fill:#8b949e;font-size:11px}
.chart .axis.end{text-anchor:end}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
footer{margin-top:32px;color:#8b949e;font-size:.8rem}
"""


def build(repo, token):
    state = load_state()
    hist = state.get("trade_history", []) or []
    balance = float(state.get("balance", 0) or 0)
    contributed = float(state.get("total_contributed", 50.0) or 50.0)
    pnl = balance - contributed
    pnl_pct = pnl / contributed * 100 if contributed else 0.0
    blob = f"https://github.com/{repo}/blob/main"

    latest = hist[-1] if hist else {}
    ref = hist[-7] if len(hist) >= 7 else (hist[0] if hist else {})
    prices = latest.get("prices", {}) or {}
    ref_prices = ref.get("prices", {}) if isinstance(ref, dict) else {}
    rows = []
    for cid, price in prices.items():
        sym = NAMES.get(cid, cid.upper())
        old = ref_prices.get(cid)
        chg = (float(price) - float(old)) / float(old) * 100 if old else None
        cls = "up" if (chg or 0) >= 0 else "down"
        chg_s = (f'<span class="{cls}">{chg:+.1f}%</span>'
                 if chg is not None else '<span class="muted">—</span>')
        rows.append(f"<tr><td><strong>{esc(sym)}</strong></td>"
                    f"<td>{esc(fmt_price(price))}</td><td>{chg_s}</td></tr>")
    price_table = ("<table><tr><th>Coin</th><th>Price</th><th>24h</th></tr>"
                   + "".join(rows) + "</table>") if rows else \
        '<p class="muted">No price data yet.</p>'

    health = run_health(repo, token)
    hrows = []
    for name in WORKFLOWS:
        if name in health:
            status, conclusion, at, url = health[name]
            if status in ("in_progress", "queued"):
                dot, txt = "run", status.replace("_", " ")
            elif conclusion == "success":
                dot, txt = "ok", "passing"
            elif conclusion == "failure":
                dot, txt = "bad", "failing"
            else:
                dot, txt = "idle", conclusion or status
            when = (at or "")[:16].replace("T", " ")
            hrows.append(
                f'<tr><td><span class="dot {dot}"></span>{esc(name)}</td>'
                f'<td>{esc(txt)}</td><td class="muted">{esc(when)}</td>'
                f'<td><a href="{esc(url)}">run</a></td></tr>')
        else:
            hrows.append(
                f'<tr><td><span class="dot idle"></span>{esc(name)}</td>'
                f'<td class="muted">no runs yet</td><td></td><td></td></tr>')
    health_table = ("<table><tr><th>Workflow</th><th>Status</th><th>Last run</th>"
                    "<th></th></tr>" + "".join(hrows) + "</table>")

    issues = open_issues(repo, token)
    if issues:
        issue_list = "<ul class='clean'>" + "".join(
            f'<li><a href="{esc(i["url"])}">#{i["number"]} {esc(i["title"])}</a>'
            + "".join(f'<span class="tag">{esc(l)}</span>' for l in i["labels"])
            + f'<div class="muted">opened {esc(i["created"])}</div></li>'
            for i in issues) + "</ul>"
    else:
        issue_list = '<p class="muted">No open issues. All quiet.</p>'

    def digest_block(folder, title):
        day, heads = digest_headlines(folder)
        if not heads:
            return f"<h2>{esc(title)}</h2><p class='muted'>No digest yet.</p>"
        items = "".join(
            f'<li><a href="{esc(url)}">{esc(t)}</a></li>' for t, url in heads)
        return (f"<h2>{esc(title)} <span class='muted'>({esc(day)})</span></h2>"
                f"<ul class='clean'>{items}</ul>"
                f"<p><a href='{blob}/{folder}/{day}.md'>Full digest →</a></p>")

    briefs = research_list()
    if briefs:
        ritems = "".join(
            f'<li><a href="{blob}/research/{esc(fn)}">{esc(topic)}</a>'
            f'<div class="muted">{esc(day)}</div></li>'
            for day, topic, fn in briefs)
        research_block = (f"<h2>Research briefs</h2><ul class='clean'>{ritems}</ul>"
                          f"<p><a href='{blob}/research/INDEX.md'>Index →</a></p>")
    else:
        research_block = ("<h2>Research briefs</h2><p class='muted'>No briefs yet — "
                          "open an issue labeled <code>research</code>.</p>")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_et = datetime.now(ET).strftime("%Y-%m-%d %I:%M %p ET")
    pnl_cls = "up" if pnl >= 0 else "down"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mission Control — paper trading fleet</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="top">
<div><h1>Mission Control</h1>
<div class="muted">Paper-trading fleet · paper only, no real money</div></div>
<div class="muted">Updated {esc(now_et)} · {esc(now_utc)}</div>
</header>

<div class="grid">
<div class="card"><div class="k">Paper balance</div><div class="v">${balance:,.2f}</div></div>
<div class="card"><div class="k">P&amp;L vs $50 start</div>
<div class="v {pnl_cls}">{pnl:+.2f} ({pnl_pct:+.1f}%)</div></div>
<div class="card"><div class="k">Tracked coins</div><div class="v">{len(prices)}</div></div>
<div class="card"><div class="k">Open issues</div><div class="v">{len(issues)}</div></div>
</div>

<section><h2>Balance history</h2>{balance_chart(hist, contributed)}</section>

<section><h2>Prices</h2>{price_table}</section>

<section><h2>Fleet health</h2>{health_table}</section>

<section><h2>Open issues</h2>{issue_list}</section>

<div class="cols">
<section>{digest_block("digests", "Crypto headlines")}</section>
<section>{digest_block("digests-ai", "AI headlines")}</section>
</div>

<section>{research_block}</section>

<footer>Built hourly by the <code>dashboard</code> workflow ·
<a href="https://github.com/{esc(repo)}">{esc(repo)}</a></footer>
</div>
</body>
</html>
"""


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("REPO", "")
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    html = build(repo, token)
    if dry_run:
        print(html)
        return 0
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w") as f:
        f.write(html)
    print(f"Wrote docs/index.html ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
