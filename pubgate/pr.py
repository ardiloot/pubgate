from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

logger = logging.getLogger(__name__)

_GH_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PRResult:
    url: str
    number: int
    created: bool


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class PRProvider(Protocol):
    def create_or_update_pr(self, *, head: str, base: str, title: str, body: str) -> PRResult: ...


# ---------------------------------------------------------------------------
# GitHub CLI implementation
# ---------------------------------------------------------------------------


class GitHubCLIProvider:
    def __init__(self, owner: str, repo: str) -> None:
        self._owner = owner
        self._repo = repo
        self._repo_slug = f"{owner}/{repo}"

    def create_or_update_pr(self, *, head: str, base: str, title: str, body: str) -> PRResult:
        existing = self._find_open_pr(head=head, base=base)
        if existing is not None:
            return self._update_pr(existing, title=title, body=body)
        return self._create_pr(head=head, base=base, title=title, body=body)

    def _gh(self, *args: str) -> str:
        cmd = ["gh", *args]
        logger.debug("gh %s", " ".join(args))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT)  # noqa: S603, S607
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}")
        return result.stdout

    def _find_open_pr(self, *, head: str, base: str) -> dict | None:
        out = self._gh(
            "pr",
            "list",
            "--repo",
            self._repo_slug,
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number,url",
            "--limit",
            "1",
        )
        prs = json.loads(out)
        if prs:
            return prs[0]
        return None

    def _create_pr(self, *, head: str, base: str, title: str, body: str) -> PRResult:
        out = self._gh(
            "pr",
            "create",
            "--repo",
            self._repo_slug,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        )
        # gh pr create outputs the PR URL on stdout
        url = out.strip()
        number = _pr_number_from_url(url)
        return PRResult(url=url, number=number, created=True)

    def _update_pr(self, existing: dict, *, title: str, body: str) -> PRResult:
        pr_number = existing["number"]
        # Use REST API instead of `gh pr edit` to avoid the projectCards
        # GraphQL deprecation bug (https://github.com/cli/cli/issues/11983).
        try:
            self._gh(
                "api",
                "--method",
                "PATCH",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{self._repo_slug}/pulls/{pr_number}",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            )
        except RuntimeError as exc:
            logger.warning("Could not update PR title/body: %s", exc)
        return PRResult(url=existing["url"], number=pr_number, created=False)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# Matches: git@github.com:owner/repo.git, git@github.com:owner/repo
_SSH_RE = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")
# Matches: https://github.com/owner/repo.git, https://github.com/owner/repo
_HTTPS_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


def parse_github_repo(url: str) -> tuple[str, str] | None:
    for pattern in (_SSH_RE, _HTTPS_RE):
        m = pattern.match(url)
        if m:
            return m.group(1), m.group(2)
    return None


# Matches: https://github.com/owner/repo/pull/123
_PR_URL_RE = re.compile(r"/pull/(\d+)$")


def _pr_number_from_url(url: str) -> int:
    m = _PR_URL_RE.search(url)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot parse PR number from URL: {url}")


# ---------------------------------------------------------------------------
# gh CLI availability check
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _gh_is_available() -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def detect_provider(remote_url: str) -> PRProvider | None:
    parsed = parse_github_repo(remote_url)
    if parsed is None:
        return None

    owner, repo = parsed

    if not _gh_is_available():
        logger.warning(
            "GitHub remote detected (%s/%s) but 'gh' CLI is not installed or "
            "not authenticated. Run 'gh auth login' to enable automatic PR creation.",
            owner,
            repo,
        )
        return None

    return GitHubCLIProvider(owner, repo)
