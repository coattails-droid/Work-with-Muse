#!/usr/bin/env python3
"""Daily crypto news digest.

Fetches RSS feeds from crypto outlets, keeps stories from roughly the last
day, dedupes by link, and writes a dated Markdown digest to digests/.
Old digests (>30 days) are pruned.

No API keys needed. Run locally or via the news-digest workflow.
"""

import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

DEFAULT_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]

FEEDS = json.loads(os.getenv("DIGEST_FEEDS", "null")) or DEFAULT_FEEDS
TITLE = os.getenv("DIGEST_TITLE", "Crypto News Digest")

DIGEST_DIR = os.getenv("DIGEST_DIR", "digests")
MAX_AGE_HOURS = int(os.getenv("DIGEST_MAX_AGE_HOURS", "36"))
MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS", "15"))
KEEP_DAYS = int(os.getenv("DIGEST_KEEP_DAYS", "30"))

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s):
    s = html.unescape(TAG_RE.sub(" ", s or ""))
    return WS_RE.sub(" ", s).strip()


def fetch_feed(name, url):
    req = urllib.request.Request(url, headers={"User-Agent": "muse-digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.findall(".//item"):
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        desc = clean_text(it.findtext("description"))[:220]
        pub_raw = it.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub_raw)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            pub = None
        if title and link:
            items.append({"source": name, "title": title, "link": link,
                          "desc": desc, "pub": pub})
    return items


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    seen, stories = set(), []
    for name, url in FEEDS:
        try:
            items = fetch_feed(name, url)
        except Exception as e:
            print(f"warning: {name} feed failed: {e}")
            continue
        for it in items:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            if it["pub"] and it["pub"] >= cutoff:
                stories.append(it)

    stories.sort(key=lambda s: s["pub"] or now, reverse=True)
    stories = stories[:MAX_ITEMS]

    day = now.strftime("%Y-%m-%d")
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, f"{day}.md")

    lines = [f"# {TITLE} — {day}",
             f"_Sources: {', '.join(n for n, _ in FEEDS)} · "
             f"generated {now.strftime('%H:%M')} UTC_\n"]
    if not stories:
        lines.append("_No fresh stories found in the window._\n")
    for s in stories:
        ago_h = int((now - s["pub"]).total_seconds() // 3600) if s["pub"] else None
        ago = f"{ago_h}h ago" if ago_h is not None else "recently"
        lines.append(f"- **[{s['title']}]({s['link']})** — {s['source']} · {ago}")
        if s["desc"]:
            lines.append(f"  > {s['desc']}")
    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # prune old digests
    for fn in os.listdir(DIGEST_DIR):
        if not fn.endswith(".md"):
            continue
        try:
            d = datetime.strptime(fn[:-3], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - d).days > KEEP_DAYS:
            os.remove(os.path.join(DIGEST_DIR, fn))
            print(f"pruned {fn}")

    print(f"wrote {path} with {len(stories)} stories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
