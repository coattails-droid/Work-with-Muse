#!/usr/bin/env python3
"""Failure detective.

Triggered by the `failure-detective` workflow when `crypto-paper-agent` fails.
Reads the failed run's logs via the GitHub API, extracts the error, matches it
against known failure patterns, and opens a GitHub issue with a diagnosis and
a suggested fix. Skips creating a duplicate if an open issue already covers the
same error signature.

Env:
    GITHUB_TOKEN - token with actions:read and issues:write
    REPO         - "owner/repo"
    RUN_ID       - failed workflow run id
"""

import hashlib
import json
import os
import re
import sys
import urllib.request

API = "https://api.github.com"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
ERROR_RE = re.compile(
    r"(error|exception|traceback|failed|failure|fatal|panic|denied|refused|"
    r"timeout|timed out|rate limit|too many requests|no space left|"
    r"could not|cannot |unable to|not found|invalid|assertionerror)",
    re.IGNORECASE,
)

# (pattern, cause title, suggested fix)
PATTERNS = [
    (re.compile(r"429|rate limit|too many requests", re.I),
     "Upstream API rate limit (HTTP 429)",
     "Add retry with exponential backoff, cache responses, or reduce the "
     "run frequency in `.github/workflows/crypto-agent.yml`."),
    (re.compile(r"ModuleNotFoundError|No module named|ImportError", re.I),
     "Missing Python dependency",
     "Add the missing package to `requirements.txt` and re-run."),
    (re.compile(r"SyntaxError", re.I),
     "Python syntax error",
     "Check the most recent commits touching `bot/` for a syntax mistake."),
    (re.compile(r"remote: Permission to .* denied|403.*resource not accessible|"
                r"GitHub Actions is not permitted", re.I),
     "GitHub token lacks permission",
     "Check the `permissions:` block in the workflow file, or the token's "
     "scopes if using a personal access token."),
    (re.compile(r"Failed to establish|NameResolution|Temporary failure in name "
                r"resolution|Max retries exceeded|Connection aborted|"
                r"ConnectionError", re.I),
     "Transient network failure",
     "Usually resolves on its own; consider adding a retry around the "
     "network call."),
    (re.compile(r"AssertionError|FAILED |assert ", re.I),
     "Test assertion failed",
     "See the failing test name in the log excerpt below and reproduce it "
     "locally."),
    (re.compile(r"No space left on device", re.I),
     "Runner disk full",
     "Clean up artifacts/caches in the workflow, or split the job."),
    (re.compile(r"The operation was canceled", re.I),
     "Job canceled or timed out",
     "Check for the 6-hour job limit or a `timeout-minutes` setting; split "
     "long jobs if needed."),
    (re.compile(r"pip.*(error|failed)|Could not find a version|"
                r"Failed building wheel", re.I),
     "Dependency install failed",
     "Pin versions in `requirements.txt` and check the package index is "
     "reachable."),
]


def api(path, token, method="GET", data=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "failure-detective",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def clean_log(text):
    return ANSI.sub("", text)


def extract_errors(log_text, max_lines=25):
    """Return the most relevant error lines from a job log."""
    lines = [ANSI.sub("", l).rstrip() for l in log_text.splitlines()]
    hits = []
    for i, line in enumerate(lines):
        if ERROR_RE.search(line) and "##[debug]" not in line:
            # keep a little context before the hit
            start = max(0, i - 1)
            for ctx in lines[start:i + 1]:
                if ctx not in hits:
                    hits.append(ctx)
        if len(hits) >= max_lines * 2:
            break
    if not hits:
        tail = [l for l in lines[-40:] if l.strip()]
        return tail[-max_lines:]
    # drop GitHub's own timestamp prefixes noise, keep order, cap length
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
        if len(out) >= max_lines:
            break
    return out


def signature(error_lines):
    norm = []
    for line in error_lines[:8]:
        line = TS.sub("<ts>", line)
        line = re.sub(r"/home/runner/work/[^ ]*", "<workdir>", line)
        line = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", line)
        norm.append(line.strip().lower())
    return hashlib.sha1("\n".join(norm).encode()).hexdigest()[:12]


def diagnose(log_text):
    for pattern, cause, fix in PATTERNS:
        if pattern.search(log_text):
            return cause, fix
    m = re.search(r"Process completed with exit code (\d+)", log_text)
    if m:
        return (f"Step exited with code {m.group(1)}",
                "Check the failing step's command and the log excerpt below "
                "for the underlying error.")
    return ("Unclassified failure",
            "Review the log excerpt below; if this recurs, add a pattern for "
            "it in `bot/failure_detective.py`.")


def find_duplicate(repo, token, sig):
    issues = api(f"/repos/{repo}/issues?state=open&per_page=100", token)
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if "[failure-detective]" in issue.get("title", "") and sig in issue.get("body", ""):
            return issue.get("html_url")
    return None


def open_issue(repo, token, title, body):
    payload = {"title": title, "body": body, "labels": ["failure-detective"]}
    try:
        return api(f"/repos/{repo}/issues", token, method="POST", data=payload)
    except Exception as e:
        if "422" in str(e):  # label may not exist yet
            payload.pop("labels")
            return api(f"/repos/{repo}/issues", token, method="POST", data=payload)
        raise


def main():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("REPO")
    run_id = os.environ.get("RUN_ID")
    if not all([token, repo, run_id]):
        print("GITHUB_TOKEN, REPO and RUN_ID must be set", file=sys.stderr)
        return 2

    run = api(f"/repos/{repo}/actions/runs/{run_id}", token)
    jobs = api(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token).get("jobs", [])
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failed:
        print("No failed jobs found; nothing to diagnose.")
        return 0

    sections = []
    all_error_text = ""
    for job in failed:
        log_text = ""
        try:
            req = urllib.request.Request(
                f"{API}/repos/{repo}/actions/jobs/{job['id']}/logs",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "failure-detective"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                log_text = clean_log(r.read().decode(errors="replace"))
        except Exception as e:
            log_text = f"<could not fetch logs: {e}>"
        all_error_text += "\n" + log_text
        failed_steps = [s["name"] for s in job.get("steps", [])
                        if s.get("conclusion") == "failure"]
        errors = extract_errors(log_text)
        excerpt = "\n".join(errors) if errors else "(no error lines found)"
        sections.append(
            f"### Job: `{job['name']}`\n"
            f"Failed steps: {', '.join(f'`{s}`' for s in failed_steps) or '(unknown)'}\n\n"
            f"<details><summary>Log excerpt</summary>\n\n```\n{excerpt[:6000]}\n```\n</details>"
        )

    cause, fix = diagnose(all_error_text)
    sig = signature(extract_errors(all_error_text))

    dup = find_duplicate(repo, token, sig)
    if dup:
        print(f"Duplicate of {dup}; skipping new issue.")
        return 0

    sha = (run.get("head_sha") or "")[:7]
    title = f"[failure-detective] {run.get('name')} failed: {cause}"
    body = (
        f"**Workflow:** `{run.get('name')}`\n"
        f"**Run:** {run.get('html_url')}\n"
        f"**Commit:** `{sha}` on `{run.get('head_branch')}`\n"
        f"**Error signature:** `{sig}`\n\n"
        f"## Likely cause\n{cause}\n\n"
        f"## Suggested fix\n{fix}\n\n"
        f"## Failed jobs\n\n" + "\n\n".join(sections) + "\n\n"
        f"---\n*Opened automatically by failure-detective. "
        f"Close this issue if it was a one-off.*"
    )
    issue = open_issue(repo, token, title, body)
    print(f"Opened {issue.get('html_url')}")
    return 0


def self_test():
    sample = """
2026-09-18T12:00:00Z ##[group]Run python bot/paper_trader.py
2026-09-18T12:00:01Z Traceback (most recent call last):
2026-09-18T12:00:01Z   File "bot/paper_trader.py", line 3, in <module>
2026-09-18T12:00:01Z     import requests
2026-09-18T12:00:01Z ModuleNotFoundError: No module named 'requests'
2026-09-18T12:00:01Z ##[error]Process completed with exit code 1.
"""
    errors = extract_errors(sample)
    assert any("ModuleNotFoundError" in e for e in errors), errors
    cause, fix = diagnose(sample)
    assert cause == "Missing Python dependency", cause
    sig = signature(errors)
    assert len(sig) == 12, sig
    # dedupe normalization: same error, different timestamps -> same signature
    other = sample.replace("2026-09-18T12:00:01Z", "2026-09-19T03:14:00Z")
    assert signature(extract_errors(other)) == sig
    print("self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        sys.exit(main())
