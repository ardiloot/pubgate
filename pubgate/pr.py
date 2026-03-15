from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

logger = logging.getLogger(__name__)

_GH_TIMEOUT = 60
_AZ_TIMEOUT = 60

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
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=_GH_TIMEOUT)  # noqa: S603, S607
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
# Azure DevOps CLI implementation
# ---------------------------------------------------------------------------


class AzureDevOpsCLIProvider:
    def __init__(self, org: str, project: str, repo: str) -> None:
        self._org = org
        self._project = project
        self._repo = repo
        self._org_url = f"https://dev.azure.com/{org}"

    def create_or_update_pr(self, *, head: str, base: str, title: str, body: str) -> PRResult:
        existing = self._find_open_pr(head=head, base=base)
        if existing is not None:
            return self._update_pr(existing, title=title, body=body)
        return self._create_pr(head=head, base=base, title=title, body=body)

    def _az(self, *args: str) -> str:
        cmd = ["az", *args]
        logger.debug("az %s", " ".join(args))
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=_AZ_TIMEOUT)  # noqa: S603, S607
        if result.returncode != 0:
            raise RuntimeError(f"az {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}")
        return result.stdout

    def _pr_url(self, pr_id: int) -> str:
        return f"https://dev.azure.com/{self._org}/{self._project}/_git/{self._repo}/pullrequest/{pr_id}"

    def _find_open_pr(self, *, head: str, base: str) -> dict | None:
        out = self._az(
            "repos",
            "pr",
            "list",
            "--org",
            self._org_url,
            "--project",
            self._project,
            "--repository",
            self._repo,
            "--source-branch",
            head,
            "--target-branch",
            base,
            "--status",
            "active",
            "--top",
            "1",
            "--detect",
            "false",
            "--output",
            "json",
        )
        prs = json.loads(out)
        if prs:
            return prs[0]
        return None

    def _create_pr(self, *, head: str, base: str, title: str, body: str) -> PRResult:
        out = self._az(
            "repos",
            "pr",
            "create",
            "--org",
            self._org_url,
            "--project",
            self._project,
            "--repository",
            self._repo,
            "--source-branch",
            head,
            "--target-branch",
            base,
            "--title",
            title,
            "--description",
            body,
            "--detect",
            "false",
            "--output",
            "json",
        )
        data = json.loads(out)
        pr_id = data["pullRequestId"]
        return PRResult(url=self._pr_url(pr_id), number=pr_id, created=True)

    def _update_pr(self, existing: dict, *, title: str, body: str) -> PRResult:
        pr_id = existing["pullRequestId"]
        try:
            self._az(
                "repos",
                "pr",
                "update",
                "--id",
                str(pr_id),
                "--org",
                self._org_url,
                "--title",
                title,
                "--description",
                body,
                "--detect",
                "false",
                "--output",
                "json",
            )
        except RuntimeError as exc:
            logger.warning("Could not update PR title/body: %s", exc)
        return PRResult(url=self._pr_url(pr_id), number=pr_id, created=False)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# GitHub
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


# Azure DevOps
# Matches: https://dev.azure.com/org/project/_git/repo[.git][/]
# Also matches with username prefix: https://org@dev.azure.com/org/project/_git/repo
_AZ_HTTPS_RE = re.compile(r"^https?://(?:[^@]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/]+?)(?:\.git)?/?$")
# Matches: git@ssh.dev.azure.com:v3/org/project/repo[.git]
_AZ_SSH_RE = re.compile(r"^git@ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$")
# Matches: user@vs-ssh.visualstudio.com:v3/org/project/repo[.git]
_AZ_SSH_LEGACY_RE = re.compile(r"^[^@]+@vs-ssh\.visualstudio\.com:v3/([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$")
# Matches: https://org.visualstudio.com/project/_git/repo[.git][/]
_AZ_HTTPS_LEGACY_RE = re.compile(r"^https?://([^.]+)\.visualstudio\.com/([^/]+)/_git/([^/]+?)(?:\.git)?/?$")


def parse_azure_devops_repo(url: str) -> tuple[str, str, str] | None:
    for pattern in (_AZ_HTTPS_RE, _AZ_SSH_RE, _AZ_SSH_LEGACY_RE, _AZ_HTTPS_LEGACY_RE):
        m = pattern.match(url)
        if m:
            return m.group(1), m.group(2), m.group(3)
    return None


# Matches: https://github.com/owner/repo/pull/123
_PR_URL_RE = re.compile(r"/pull/(\d+)$")


def _pr_number_from_url(url: str) -> int:
    m = _PR_URL_RE.search(url)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot parse PR number from URL: {url}")


# ---------------------------------------------------------------------------
# CLI availability checks
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _gh_is_available() -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GH_TIMEOUT,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_az_devops_extension() -> None:
    """Install the azure-devops extension if not already present."""
    try:
        subprocess.run(  # noqa: S603, S607
            ["az", "extension", "add", "--name", "azure-devops", "--only-show-errors", "--yes"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_AZ_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


@lru_cache(maxsize=1)
def _az_devops_is_available() -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["az", "account", "show"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_AZ_TIMEOUT,
        )
        if result.returncode != 0:
            return False
        _ensure_az_devops_extension()
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def detect_provider(remote_url: str) -> PRProvider | None:
    # Try GitHub
    gh_parsed = parse_github_repo(remote_url)
    if gh_parsed is not None:
        owner, repo = gh_parsed
        if not _gh_is_available():
            logger.warning(
                "GitHub remote detected (%s/%s) but 'gh' CLI is not installed or "
                "not authenticated. Run 'gh auth login' to enable automatic PR creation.",
                owner,
                repo,
            )
            return None
        return GitHubCLIProvider(owner, repo)

    # Try Azure DevOps
    az_parsed = parse_azure_devops_repo(remote_url)
    if az_parsed is not None:
        org, project, repo = az_parsed
        if not _az_devops_is_available():
            logger.warning(
                "Azure DevOps remote detected (%s/%s/%s) but 'az' CLI is not installed or "
                "not authenticated. Run 'az login' to enable automatic PR creation.",
                org,
                project,
                repo,
            )
            return None
        return AzureDevOpsCLIProvider(org, project, repo)

    return None
