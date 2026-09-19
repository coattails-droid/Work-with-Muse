#!/usr/bin/env python3
"""Research agent.

Triggered by the `research-agent` workflow when a GitHub issue labeled
`research` (or titled "Research: ...") is opened/labeled, or manually via
workflow_dispatch with a topic.

It runs a web research pass (Wikipedia API, Google News RSS, and
best-effort DuckDuckGo web search plus direct page fetches — stdlib only),
writes an extractive research brief to research/YYYY-MM-DD-<slug>.md, updates
research/INDEX.md, and comments the brief summary back on the issue.

This is a v1 extractive agent: it gathers sources and surfaces the most
relevant sentences per sub-question. It does not call an LLM, so the brief
organizes evidence rather than synthesizing conclusions. Verify key claims
before acting on them.

Env:
    GITHUB_TOKEN - token with issues:write (only needed to comment on issues)
    REPO         - "owner/repo"
    TOPIC        - research topic (workflow_dispatch); falls back to the issue
    ISSUE_NUMBER - issue to report back to (optional)
    ISSUE_TITLE  - used for the topic when TOPIC is empty
    ISSUE_BODY   - used as the research question context
    DRY_RUN=1    - print the report instead of writing files / commenting
"""

import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPORT_MARKER = "<!-- research-agent-report -->"
MAX_PAGES = 12          # max web pages fetched per run
PER_QUERY_PAGES = 3     # pages kept per sub-question
SOFT_DEADLINE_SECS = 7 * 60
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 research-agent/1.0")

STOPWORDS = {
    "about", "after", "again", "against", "among", "because", "before",
    "being", "between", "both", "does", "doing", "down", "during", "each",
    "from", "further", "having", "into", "more", "most", "other", "over",
    "same", "such", "than", "that", "then", "there", "these", "they",
    "this", "those", "through", "under", "until", "what", "when", "where",
    "which", "while", "with", "within", "would", "your",
}
BOILERPLATE_RE = re.compile(
    r"cookie|subscribe|newsletter|sign in|log in|sign up|all rights reserved|"
    r"privacy policy|terms of service|javascript is disabled|enable javascript|"
    r"skip to main content|on this page|table of contents|"
    r"learn more|our programs|partner program|elevate your|transform the future|"
    r"trusted partner|click here|"
    r"svg\]|hsl\(var|signed in with another tab|trademark of",
    re.IGNORECASE,
)


def api(path, token, method="GET", data=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "research-agent",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def http_get(url, timeout=20, max_bytes=1_500_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes)


def html_to_text(html):
    text = re.sub(r"(?s)<!--.*?-->", " ", html)  # strip HTML comments first
    text = re.sub(r"(?is)<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    # RSS descriptions are sometimes double-escaped; unescape repeatedly,
    # then strip any tags the unescaping revealed (letter after < avoids
    # eating things like "5 < 10").
    for _ in range(3):
        new = htmlmod.unescape(text)
        if new == text:
            break
        text = new
    text = re.sub(r"(?s)</?[a-zA-Z][^>]*>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def ddg_search(query, max_results=6):
    """Search via DuckDuckGo lite; return [(title, url)]."""
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    try:
        html = http_get(url).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  search failed for {query!r}: {e}", file=sys.stderr)
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'<a rel="nofollow" href="//duckduckgo\.com/l/\?uddg=([^"&]+)[^>]*>(.*?)</a>',
            html, re.DOTALL):
        target = urllib.parse.unquote(m.group(1))
        title = html_to_text(m.group(2)).strip()[:160]
        if not target.startswith("http") or "duckduckgo.com" in target:
            continue
        if target in seen:
            continue
        seen.add(target)
        results.append((title or target, target))
        if len(results) >= max_results:
            break
    return results


def wiki_search(query, max_results=3):
    """Search Wikipedia; return [(title, url)]. Keyless and reliable."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
           "&srsearch=" + urllib.parse.quote(query) +
           f"&srlimit={max_results}&format=json")
    try:
        data = json.loads(http_get(url).decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  wiki search failed for {query!r}: {e}", file=sys.stderr)
        return []
    out = []
    for item in data.get("query", {}).get("search", []):
        title = item["title"]
        out.append((title,
                    "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")))
    return out


def wiki_extract(title, max_chars=6000):
    """Fetch a clean plaintext extract of a Wikipedia article."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
           "&explaintext=1&exsectionformat=plain&titles=" +
           urllib.parse.quote(title) + "&format=json")
    try:
        data = json.loads(http_get(url).decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  wiki extract failed for {title!r}: {e}", file=sys.stderr)
        return None
    for page in data.get("query", {}).get("pages", {}).values():
        text = page.get("extract", "")
        if len(text) > 400:
            return {"url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                    "title": f"{title} — Wikipedia",
                    "text": text[:max_chars]}
    return None


def gnews_search(query, max_results=5):
    """Search Google News RSS (keyless); return [(title, url, pubdate)]."""
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query) +
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        xml = http_get(url).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  news search failed for {query!r}: {e}", file=sys.stderr)
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:max_results]:
        title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        desc = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
        if title and link:
            desc_text = html_to_text(desc.group(1)).strip() if desc else ""
            out.append((htmlmod.unescape(title.group(1)).strip(),
                        link.group(1).strip(),
                        pub.group(1).strip() if pub else "",
                        " ".join(desc_text.split())[:600]))
    return out


def find_page(pages, url):
    for p in pages:
        if p["url"] == url:
            return p
    return None


def gather_section(query, kind, pages, start):
    """Collect up to PER_QUERY_PAGES pages for one sub-question.

    Pages already gathered for an earlier section are referenced, not
    re-fetched, so later sections can draw on the same sources.

    kind: 'encyclopedic' (Wikipedia first), 'news' (Google News RSS first),
    or 'critical' (Wikipedia then broad web). DuckDuckGo is best-effort
    everywhere since it rate-limits bots.
    """
    section_pages = []

    def take(page):
        if page is None or len(section_pages) >= PER_QUERY_PAGES:
            return
        existing = find_page(pages, page["url"])
        if existing is not None:
            if all(p["url"] != existing["url"] for p in section_pages):
                section_pages.append(existing)
            return
        if len(pages) >= MAX_PAGES:
            return
        pages.append(page)
        section_pages.append(page)

    if kind in ("encyclopedic", "critical"):
        time.sleep(1)
        for title, url in wiki_search(query):
            if len(section_pages) >= PER_QUERY_PAGES:
                break
            existing = find_page(pages, url)
            take(existing if existing is not None else wiki_extract(title))

    if kind == "news":
        time.sleep(1)
        for title, url, pub, desc in gnews_search(query):
            if len(section_pages) >= PER_QUERY_PAGES:
                break
            if time.time() - start > SOFT_DEADLINE_SECS:
                break
            existing = find_page(pages, url)
            if existing is not None:
                take(existing)
                continue
            page = fetch_page(url)
            label = f"{title} [{pub}]" if pub else title
            if page:
                page["title"] = label
            else:
                # fall back to the RSS description snippet when the
                # publisher page can't be fetched (paywall / bot block)
                text = f"{title}. {desc}".strip()
                if len(text) < 120:
                    continue
                page = {"url": url, "title": label, "text": text}
            take(page)

    if (len(section_pages) < PER_QUERY_PAGES
            and time.time() - start < SOFT_DEADLINE_SECS):
        time.sleep(1)
        for _title, url in ddg_search(query):
            if len(section_pages) >= PER_QUERY_PAGES:
                break
            existing = find_page(pages, url)
            if existing is not None:
                take(existing)
                continue
            if len(pages) >= MAX_PAGES:
                break
            take(fetch_page(url))

    return section_pages


def fetch_page(url):
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  fetch failed {url}: {e}", file=sys.stderr)
        return None
    if "<html" not in raw[:2000].lower() and "<!doctype" not in raw[:2000].lower():
        return None
    text = html_to_text(raw)
    if len(text) < 400:  # probably a stub / JS shell
        return None
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if m:
        title = html_to_text(m.group(1)).strip()[:160]
    return {"url": url, "title": title or url, "text": text[:6000]}


def keywords(topic):
    words = re.findall(r"[a-z0-9]+", topic.lower())
    return {w for w in words if len(w) >= 4 and w not in STOPWORDS}


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for p in parts:
        p = " ".join(p.split())  # collapse internal newlines/whitespace
        words = p.split()
        if 8 <= len(words) <= 60 and re.search(r"[a-zA-Z]", p):
            if not BOILERPLATE_RE.search(p):
                if not re.search(r"[→×▲▼►◄]", p) and not p[:1] in "-–•·":
                    out.append(p)
    return out


def score_sentence(sent, kw, position):
    words = re.findall(r"[a-z0-9]+", sent.lower())
    if not words:
        return 0
    hits = sum(1 for w in words if w in kw)
    bonus = 0.5 if position < 3 else 0
    return hits / (len(words) ** 0.5) + bonus


def pick_sentences(pages, kw, n, exclude=(), require_any=None, allowed=None):
    """Extractive pick over the global pages list.

    allowed: optional set of page URLs to restrict the pick to (per-section
    scoping). Returned indices always refer to the global pages list so
    source references stay correct.
    """
    excluded = {" ".join(s.split()).lower() for s in exclude}
    req = {w.lower() for w in (require_any or set())}
    scored = []
    for pi, page in enumerate(pages):
        if allowed is not None and page["url"] not in allowed:
            continue
        for pos, sent in enumerate(split_sentences(page["text"])):
            if sent.lower() in excluded:
                continue
            if req and not req.intersection(
                    re.findall(r"[a-z0-9]+", sent.lower())):
                continue
            scored.append((score_sentence(sent, kw, pos), pi, sent))
    scored.sort(key=lambda t: -t[0])
    picked, seen_tokens = [], set()
    for score, pi, sent in scored:
        if score <= 0:
            continue
        tokens = frozenset(re.findall(r"[a-z0-9]+", sent.lower()))
        if any(len(tokens & st) / max(len(tokens), 1) > 0.8 for st in seen_tokens):
            continue
        seen_tokens.add(tokens)
        picked.append((pi, sent))
        if len(picked) >= n:
            break
    return picked


def sub_questions(topic):
    # (section title, query, backend kind, extra scoring keywords,
    #  required keywords — a picked sentence must contain at least one)
    mech = {"node", "nodes", "packet", "packets", "transmit", "signal",
            "frequency", "protocol", "device", "devices", "mesh", "network"}
    limit = {"limitation", "limitations", "however", "problem", "critic",
             "concern", "concerns", "drawback", "limited", "disadvantage"}
    return [
        ("Overview", topic, "encyclopedic", set(), None),
        ("How it works", topic, "encyclopedic", mech, mech),
        ("Latest developments", topic, "news",
         {"announced", "released", "launch", "update", "version",
          "2025", "2026"}, None),
        ("Limitations and criticism", topic, "critical", limit, limit),
    ]


# Specific sections pick before generic ones so "Overview" doesn't eat the
# mechanism/criticism sentences they need. Display order is unchanged.
PICK_ORDER = [1, 3, 0, 2]


def research(topic):
    kw = keywords(topic)
    pages = []  # dicts with url/title/text
    start = time.time()
    questions = sub_questions(topic)

    # Gather pages for every section first (display order).
    gathered = []
    for section, query, kind, extra, req in questions:
        if time.time() - start > SOFT_DEADLINE_SECS:
            print("  soft deadline reached; stopping gathering", file=sys.stderr)
            break
        print(f"Searching ({kind}): {query}", flush=True)
        section_pages = gather_section(query, kind, pages, start)
        gathered.append((section, query, extra, req, section_pages))

    # Pick sentences most-specific-first so generic sections don't starve
    # the specialized ones; de-duplicate across sections as we go.
    picked = {}
    used = set()
    for idx in PICK_ORDER:
        if idx >= len(gathered):
            continue
        section, query, extra, req, section_pages = gathered[idx]
        allowed = {p["url"] for p in section_pages}
        sents = pick_sentences(pages, kw | keywords(query) | extra, 4,
                               require_any=req, allowed=allowed)
        fresh = [(pi, s) for pi, s in sents if s.lower() not in used]
        used.update(s.lower() for _, s in fresh)
        picked[section] = fresh

    final_sections = [(q[0], picked.get(q[0], [])) for q in questions]
    top = pick_sentences(pages, kw, 6,
                         exclude=[s for sents in picked.values()
                                  for _, s in sents])
    return pages, final_sections, top


def slugify(topic):
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug[:60] or "research"


def build_report(topic, question, pages, per_section, top, issue_number, date):
    lines = [
        f"# Research: {topic}",
        "",
        f"_Date: {date} · Sources: {len(pages)} web pages · "
        f"Issue: #{issue_number if issue_number else 'n/a'}_",
        "",
    ]
    if question:
        lines += ["## Question", "", question.strip()[:2000], ""]
    lines += ["## Key findings", ""]
    if top:
        for pi, sent in top:
            lines.append(f"- {sent} [{pi + 1}]")
    else:
        lines.append("- No substantive findings extracted; see sources below.")
    lines += [""]
    for section, sents in per_section:
        lines += [f"## {section}", ""]
        if sents:
            for pi, sent in sents:
                lines.append(f"- {sent} [{pi + 1}]")
        else:
            lines.append("- _No strong excerpts found for this angle._")
        lines += [""]
    lines += ["## Sources", ""]
    for i, p in enumerate(pages):
        lines.append(f"{i + 1}. {p['title']} — {p['url']}")
    lines += [
        "",
        "## Method note",
        "",
        "Extractive brief assembled automatically by research-agent v1: it "
        "ran web searches, fetched source pages, and surfaced the most "
        "topic-relevant sentences. It does not verify claims or synthesize "
        "conclusions — verify key claims against the sources before acting "
        "on them.",
        "",
    ]
    return "\n".join(lines)


def update_index(date, topic, filename, issue_number):
    path = os.path.join("research", "INDEX.md")
    header = "# Research index\n\n| Date | Topic | Report | Issue |\n|---|---|---|---|\n"
    row = (f"| {date} | {topic} | [{os.path.basename(filename)}]"
           f"({os.path.basename(filename)}) | "
           f"{'#' + issue_number if issue_number else 'n/a'} |\n")
    if os.path.exists(path):
        with open(path) as f:
            content = f.read()
        if not content.endswith("\n"):
            content += "\n"
        content += row
    else:
        content = header + row
    with open(path, "w") as f:
        f.write(content)


def already_reported(repo, token, issue_number):
    try:
        comments = api(f"/repos/{repo}/issues/{issue_number}/comments?per_page=100",
                       token)
    except Exception:
        return False
    return any(REPORT_MARKER in (c.get("body") or "") for c in comments)


def comment_issue(repo, token, issue_number, body):
    payload = {"body": body}
    return api(f"/repos/{repo}/issues/{issue_number}/comments", token,
               method="POST", data=payload)


def ensure_label(repo, token, issue_number, label="research"):
    try:
        api(f"/repos/{repo}/issues/{issue_number}/labels", token,
            method="POST", data={"labels": [label]})
    except Exception as e:
        if "422" not in str(e):
            print(f"  label add failed: {e}", file=sys.stderr)


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("REPO", "")
    topic = os.environ.get("TOPIC", "").strip()
    issue_number = os.environ.get("ISSUE_NUMBER", "").strip()
    issue_title = os.environ.get("ISSUE_TITLE", "")
    issue_body = os.environ.get("ISSUE_BODY", "")
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    if not topic and issue_title:
        topic = re.sub(r"^(research\s*:\s*|\[research\]\s*)", "",
                       issue_title, flags=re.IGNORECASE).strip()
    if not topic:
        print("No topic: set TOPIC or trigger from an issue", file=sys.stderr)
        return 2
    question = issue_body.strip() or topic

    if issue_number and token and not dry_run:
        if already_reported(repo, token, issue_number):
            print(f"Issue #{issue_number} already has a research report; skipping.")
            return 0

    date = time.strftime("%Y-%m-%d", time.gmtime())
    print(f"Researching: {topic}", flush=True)
    pages, per_section, top = research(topic)
    print(f"Gathered {len(pages)} pages", flush=True)

    filename = f"{date}-{slugify(topic)}.md"
    report = build_report(topic, question, pages, per_section, top,
                          issue_number, date)

    if dry_run:
        print("\n" + "=" * 60 + "\n")
        print(report)
        return 0

    os.makedirs("research", exist_ok=True)
    with open(os.path.join("research", filename), "w") as f:
        f.write(report)
    update_index(date, topic, filename, issue_number)
    print(f"Wrote research/{filename}")

    if issue_number and token:
        bullets = "\n".join(f"- {s}" for _, s in top[:6]) or \
            "- _No findings extracted; see the full report._"
        comment = (
            f"{REPORT_MARKER}\n"
            f"## Research brief: {topic}\n\n"
            f"{bullets}\n\n"
            f"**Full report:** `research/{filename}` "
            f"({len(pages)} sources, committed to the repo)\n\n"
            f"_Extractive brief assembled automatically; verify key claims "
            f"against the sources before acting on them._"
        )
        comment_issue(repo, token, issue_number, comment)
        ensure_label(repo, token, issue_number)
        print(f"Commented on issue #{issue_number}")
    return 0


def self_test():
    # sentence scoring prefers keyword-dense, well-formed sentences
    kw = {"meshtastic", "radio"}
    sents = pick_sentences(
        [{"text": ("Meshtastic is an open source mesh radio project. "
                   "I like cookies and subscribe to newsletters. "
                   "The Meshtastic radio firmware runs on LoRa hardware.")}],
        kw, 2)
    assert len(sents) == 2, sents
    assert all("Meshtastic" in s or "radio" in s for _, s in sents), sents
    # boilerplate filtered
    assert not any("cookies" in s for _, s in sents), sents
    # slugify
    assert slugify("Meshtastic Mesh Radios!") == "meshtastic-mesh-radios"
    # html_to_text strips scripts
    t = html_to_text("<html><head><script>var x=1;</script></head>"
                     "<body><p>Hello world</p></body></html>")
    assert "var x" not in t and "Hello world" in t, t
    print("self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        sys.exit(main())
