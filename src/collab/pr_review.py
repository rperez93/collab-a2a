"""Explicit commit evidence, never an approval or an atomic merge check."""
from __future__ import annotations
import re
import shlex
import subprocess

FIELDS = ("reviewed_sha", "tested_sha", "head_sha")


def commit_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise ValueError("commit SHA must be a full 40 or 64 character hexadecimal hash")
    return value.lower()


def status(pr):
    head = pr.get("head_sha")
    reviewed = pr.get("reviewed_sha")
    tested = pr.get("tested_sha")
    if head and reviewed and head != reviewed:
        return "WARNING: observed PR head differs from reviewed SHA"
    if head and tested and head != tested:
        return "WARNING: observed PR head differs from tested SHA"
    if not reviewed:
        return "review SHA unknown"
    if not tested:
        return "test SHA unknown"
    if reviewed != tested:
        return "WARNING: reviewed and tested SHAs differ"
    if not head:
        return "PR head unknown; check before merge"
    return "review and tests match observed head; recheck before merge"


def task_warning(task):
    warnings = [status(pr) for pr in task.get("prs", []) if status(pr).startswith("WARNING:")]
    return " | ".join(dict.fromkeys(warnings))


def github_head(url):
    # Never pass arbitrary peer URLs or shell syntax to gh. Explicit provider
    # and path keep credentials on github.com even if GH_HOST is configured.
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", url)
    if not match:
        raise ValueError("--check-head requires a github.com pull request URL; use --head-sha for another forge")
    from collab.source_command import run
    owner, repo, number = match.groups()
    if owner in (".", "..") or repo in (".", ".."):
        raise ValueError("invalid GitHub repository path")
    # Replace the shell so the waited child is gh itself, not an exited shell
    # whose killed child would be left for PID 1 to reap.
    command = "exec " + shlex.join(["gh", "api", "--hostname", "github.com", f"repos/{owner}/{repo}/pulls/{number}", "--jq", ".head.sha"])
    try:
        result = run(command, timeout=10, max_bytes=4096)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("could not check PR head within the 10 second / 4 KiB limit") from exc
    if result.returncode:
        raise ValueError("could not check PR head; verify gh authentication and repository access")
    return commit_sha(result.stdout.strip())
