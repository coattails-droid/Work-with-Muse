#!/usr/bin/env python3
"""Local open-model watcher.

Finds recently-created open models gaining traction on Hugging Face and
appends a "new open models for local hardware" section to today's AI digest.
Already-reported models are tracked in `.seen_models.json` inside the digest
dir, so each model is reported once.

No API keys needed.

Env:
    DIGEST_DIR - default "digests-ai"
    HF_DAYS    - freshness window in days, default 14
    HF_PAGES   - API pages (100 models each) to scan, default 5
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

DIGEST_DIR = os.getenv("DIGEST_DIR", "digests-ai")
HF_DAYS = int(os.getenv("HF_DAYS", "14"))
HF_PAGES = int(os.getenv("HF_PAGES", "5"))
MAX_MODELS = 8

SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")


def hf_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "muse-models/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), r.headers.get("Link")


def fetch_models():
    models, url = [], ("https://huggingface.co/api/models"
                       "?sort=likes&direction=-1&limit=100")
    for _ in range(HF_PAGES):
        if not url:
            break
        batch, link = hf_get(url)
        models.extend(batch)
        url = None
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1:part.find(">")]
    return models


def size_hint(model_id):
    m = SIZE_RE.search(model_id)
    if not m:
        return None, "check hardware needs"
    b = float(m.group(1))
    label = f"~{m.group(1)}B params"
    if b <= 8:
        fit = "fits most laptops"
    elif b <= 32:
        fit = "needs a decent GPU"
    elif b <= 70:
        fit = "needs a beefy GPU"
    else:
        fit = "datacenter-class"
    return label, fit


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=HF_DAYS)

    fresh = []
    for m in fetch_models():
        if m.get("disabled"):
            continue
        try:
            created = datetime.fromisoformat(
                m["createdAt"].replace("Z", "+00:00"))
        except Exception:
            continue
        if created >= cutoff:
            fresh.append((m, created))
    fresh.sort(key=lambda mc: mc[0].get("likes", 0), reverse=True)

    seen_path = os.path.join(DIGEST_DIR, ".seen_models.json")
    try:
        with open(seen_path) as f:
            seen = set(json.load(f))
    except Exception:
        seen = set()

    new_models = []
    for m, created in fresh:
        if m["id"] in seen or len(new_models) >= MAX_MODELS:
            continue
        new_models.append((m, created))
        seen.add(m["id"])

    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(seen_path, "w") as f:
        json.dump(sorted(seen), f, indent=2)

    if not new_models:
        print("no new models to report")
        return 0

    day = now.strftime("%Y-%m-%d")
    path = os.path.join(DIGEST_DIR, f"{day}.md")
    lines = []
    if not os.path.exists(path):
        lines.append(f"# AI News Digest — {day}\n")

    lines.append("## New open models for local hardware")
    lines.append("_Fresh open weights gaining traction on Hugging Face. "
                 "Size guide is rough — GGUF quants vary._\n")
    for m, created in new_models:
        size, fit = size_hint(m["id"])
        bits = [f"{m.get('likes', 0):,} likes"]
        if size:
            bits.append(size)
        bits.append(fit)
        if "gguf" in (m.get("tags") or []):
            bits.append("GGUF quants available")
        if m.get("gated"):
            bits.append("gated access")
        bits.append(created.strftime("%b %d"))
        lines.append(f"- **[{m['id']}](https://huggingface.co/{m['id']})**"
                     f" — {' · '.join(bits)}")
    lines.append("")

    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")

    print(f"appended {len(new_models)} models to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
