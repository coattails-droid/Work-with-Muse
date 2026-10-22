#!/usr/bin/env python3
"""Append one JSON line per pushed commit to ledger/changes.jsonl.

Run by .github/workflows/auto-changelog.yml on every push to main.
Each line is a self-contained JSON object (JSON Lines format), newest last.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path("ledger/changes.jsonl")
ZERO = "0" * 40


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


def commits_to_log(last_logged: str, before: str, after: str) -> list[str]:
    # Prefer everything since the last ledger entry, so commits pushed by bots
    # with GITHUB_TOKEN (which don't trigger workflows) are still captured.
    if last_logged and is_ancestor(last_logged, after):
        out = git("rev-list", "--reverse", f"{last_logged}..{after}")
        return [s for s in out.split() if s]
    if before and before != ZERO:
        try:
            out = git("rev-list", "--reverse", f"{before}..{after}")
            shas = [s for s in out.split() if s]
            if shas:
                return shas
        except subprocess.CalledProcessError:
            pass
    return [after]


def describe(sha: str) -> dict:
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%P%x1f%B"
    h, an, ae, ad, cn, parents, body = git("show", "-s", f"--format={fmt}", sha).split("\x1f", 6)
    files = []
    for line in git("show", "--format=", "--name-status", "--no-renames", sha).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[-1]})
    message = body.strip()
    return {
        "sha": h,
        "short_sha": h[:7],
        "authored_at": ad,
        "author_name": an,
        "author_email": ae,
        "committer_name": cn,
        "parents": parents.split(),
        "summary": message.splitlines()[0] if message else "",
        "message": message,
        "files_changed": len(files),
        "files": files[:500],
        "files_truncated": len(files) > 500,
    }


def main() -> None:
    before = os.environ.get("PUSH_BEFORE", "")
    after = os.environ.get("PUSH_AFTER") or git("rev-parse", "HEAD").strip()
    pusher = os.environ.get("PUSHER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "")
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    seen = set()
    last_logged = ""
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                last_logged = json.loads(line)["sha"]
                seen.add(last_logged)
            except Exception:
                continue

    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with LEDGER.open("a", encoding="utf-8") as f:
        for sha in commits_to_log(last_logged, before, after):
            entry = describe(sha)
            if entry["sha"] in seen or entry["summary"].startswith("chore: update changelog"):
                continue
            entry.update({"repo": repo, "branch": ref, "pushed_by": pusher,
                          "recorded_at": recorded_at, "run_url": run_url, "schema": 1})
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            added += 1
    print(f"Appended {added} commit(s) to {LEDGER}")


if __name__ == "__main__":
    main()
