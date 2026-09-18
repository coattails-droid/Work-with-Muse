#!/usr/bin/env python3
"""Price sentinel.

Runs on a schedule. Fetches 24h price changes for tracked coins from
CoinGecko and manages GitHub issues as alerts:

  * coin moves >= ALERT_THRESHOLD_PCT (abs, 24h) -> open an issue (or comment
    on the existing open one with a fresh reading)
  * coin settles back under half the threshold -> close the issue with a note

Each issue body carries an HTML marker `<!-- sentinel-coin: <id> -->` so the
script can map open alerts back to coins.

Env:
    GITHUB_TOKEN        - token with issues:write
    REPO                - "owner/repo"
    COIN_IDS            - comma-separated CoinGecko ids (default: the big five)
    ALERT_THRESHOLD_PCT - alert threshold, percent (default: 5)
    DRY_RUN             - "1" to print actions without touching GitHub
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

API = "https://api.github.com"
MARKER_RE = re.compile(r"<!-- sentinel-coin: ([a-z0-9-]+) -->")


def api(path, token, method="GET", data=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "price-sentinel",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def fetch_markets(coin_ids):
    ids = urllib.parse.quote(coin_ids)
    url = (f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
           f"&ids={ids}&price_change_percentage=24h")
    req = urllib.request.Request(url, headers={"User-Agent": "price-sentinel"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fmt_usd(x):
    if x is None:
        return "n/a"
    if x < 1:
        return f"${x:,.4f}"
    return f"${x:,.2f}"


def decide_actions(markets, open_alerts, threshold):
    """Pure logic: map market data + open alert issues to actions.

    Returns list of dicts: {"kind": "open"|"comment"|"close", ...}.
    """
    by_id = {c["id"]: c for c in markets}
    actions = []
    for cid, coin in by_id.items():
        chg = coin.get("price_change_percentage_24h") or 0.0
        issue = open_alerts.get(cid)
        if abs(chg) >= threshold:
            direction = "up" if chg > 0 else "down"
            if issue:
                actions.append({"kind": "comment", "issue": issue, "coin": coin,
                                "change": chg, "direction": direction})
            else:
                actions.append({"kind": "open", "coin": coin,
                                "change": chg, "direction": direction})
        elif issue and abs(chg) < threshold / 2:
            actions.append({"kind": "close", "issue": issue, "coin": coin,
                            "change": chg})
    return actions


def alert_title(coin, change, direction):
    arrow = "▲" if direction == "up" else "▼"
    return (f"[price-sentinel] {coin['symbol'].upper()} {arrow} "
            f"{change:+.1f}% in 24h")


def alert_body(coin, change, direction):
    word = "surged" if direction == "up" else "dropped"
    return (
        f"<!-- sentinel-coin: {coin['id']} -->\n"
        f"**{coin['name']} ({coin['symbol'].upper()})** {word} "
        f"**{change:+.2f}%** over the last 24h.\n\n"
        f"- Current price: **{fmt_usd(coin.get('current_price'))}**\n"
        f"- 24h high / low: {fmt_usd(coin.get('high_24h'))} / "
        f"{fmt_usd(coin.get('low_24h'))}\n\n"
        f"This issue stays open while the 24h move remains elevated and "
        f"closes automatically once it settles.\n\n"
        f"---\n*Opened automatically by price-sentinel.*"
    )


def comment_body(coin, change, direction):
    word = "up" if direction == "up" else "down"
    return (f"Update: still {word} **{change:+.2f}%** over 24h — now "
            f"**{fmt_usd(coin.get('current_price'))}**.")


def main():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("REPO")
    if not all([token, repo]):
        print("GITHUB_TOKEN and REPO must be set", file=sys.stderr)
        return 2
    coin_ids = os.environ.get(
        "COIN_IDS", "bitcoin,ethereum,ripple,bitcoin-cash,kaspa")
    threshold = float(os.environ.get("ALERT_THRESHOLD_PCT", "5"))
    dry_run = os.environ.get("DRY_RUN") == "1"

    try:
        markets = fetch_markets(coin_ids)
    except Exception as e:
        print(f"CoinGecko fetch failed: {e}", file=sys.stderr)
        return 1
    if not isinstance(markets, list) or not markets:
        print("CoinGecko returned no market data", file=sys.stderr)
        return 1

    issues = api(f"/repos/{repo}/issues?state=open&per_page=100", token)
    open_alerts = {}
    if isinstance(issues, list):
        for issue in issues:
            if issue.get("pull_request"):
                continue
            m = MARKER_RE.search(issue.get("body") or "")
            if m and "[price-sentinel]" in issue.get("title", ""):
                open_alerts[m.group(1)] = issue

    actions = decide_actions(markets, open_alerts, threshold)
    if not actions:
        print("All quiet: no coin beyond threshold, no alerts to close.")
        return 0

    for a in actions:
        coin = a["coin"]
        if a["kind"] == "open":
            title = alert_title(coin, a["change"], a["direction"])
            body = alert_body(coin, a["change"], a["direction"])
            if dry_run:
                print(f"DRY-RUN open: {title}")
                continue
            issue = api(f"/repos/{repo}/issues", token, method="POST",
                        data={"title": title, "body": body,
                              "labels": ["price-sentinel"]})
            print(f"Opened {issue.get('html_url')}")
        elif a["kind"] == "comment":
            body = comment_body(coin, a["change"], a["direction"])
            num = a["issue"]["number"]
            if dry_run:
                print(f"DRY-RUN comment on #{num}: {body}")
                continue
            api(f"/repos/{repo}/issues/{num}/comments", token,
                method="POST", data={"body": body})
            print(f"Commented on #{num}")
        elif a["kind"] == "close":
            num = a["issue"]["number"]
            body = (f"Settled: 24h move back to **{a['change']:+.2f}%** "
                    f"(under half the alert threshold). Closing.")
            if dry_run:
                print(f"DRY-RUN close #{num}")
                continue
            api(f"/repos/{repo}/issues/{num}/comments", token,
                method="POST", data={"body": body})
            api(f"/repos/{repo}/issues/{num}", token, method="PATCH",
                data={"state": "closed"})
            print(f"Closed #{num}")
    return 0


def self_test():
    markets = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
         "current_price": 82000, "high_24h": 83000, "low_24h": 76000,
         "price_change_percentage_24h": 7.5},
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum",
         "current_price": 2500, "high_24h": 2600, "low_24h": 2400,
         "price_change_percentage_24h": 1.2},
        {"id": "ripple", "symbol": "xrp", "name": "XRP",
         "current_price": 1.30, "high_24h": 1.45, "low_24h": 1.28,
         "price_change_percentage_24h": -6.1},
    ]
    # no open alerts: btc opens, xrp opens, eth quiet
    actions = decide_actions(markets, {}, 5.0)
    kinds = sorted(a["kind"] for a in actions)
    assert kinds == ["open", "open"], kinds
    assert "▲" in alert_title(markets[0], 7.5, "up")
    assert "▼" in alert_title(markets[2], -6.1, "down")
    assert "<!-- sentinel-coin: bitcoin -->" in alert_body(markets[0], 7.5, "up")

    # existing btc alert -> comment instead of open; calm eth alert -> close
    fake_issue = lambda n, cid: {"number": n, "title": "[price-sentinel] x",
                                "body": f"<!-- sentinel-coin: {cid} -->"}
    open_alerts = {"bitcoin": fake_issue(1, "bitcoin"),
                   "ethereum": fake_issue(2, "ethereum")}
    actions = decide_actions(markets, open_alerts, 5.0)
    by_kind = {}
    for a in actions:
        by_kind.setdefault(a["kind"], []).append(a["coin"]["id"])
    assert by_kind["comment"][0] == "bitcoin", by_kind
    assert by_kind["close"][0] == "ethereum", by_kind
    assert by_kind["open"][0] == "ripple", by_kind
    print("self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        sys.exit(main())
