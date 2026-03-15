import json
import logging
import subprocess
from unittest.mock import patch

import pytest
from conftest import Topology

from pubgate.pr import (
    GitHubCLIProvider,
    PRResult,
    _pr_number_from_url,
    detect_provider,
    parse_github_repo,
)

# ---------------------------------------------------------------------------
# parse_github_repo
# ---------------------------------------------------------------------------


class TestParseGithubRepo:
    def test_ssh_url(self):
        assert parse_github_repo("git@github.com:owner/repo.git") == ("owner", "repo")

    def test_ssh_url_no_dot_git(self):
        assert parse_github_repo("git@github.com:owner/repo") == ("owner", "repo")

    def test_https_url(self):
        assert parse_github_repo("https://github.com/owner/repo.git") == ("owner", "repo")

    def test_https_url_no_dot_git(self):
        assert parse_github_repo("https://github.com/owner/repo") == ("owner", "repo")

    def test_https_url_trailing_slash(self):
        assert parse_github_repo("https://github.com/owner/repo/") == ("owner", "repo")

    def test_http_url(self):
        assert parse_github_repo("http://github.com/owner/repo.git") == ("owner", "repo")

    def test_non_github_ssh(self):
        assert parse_github_repo("git@gitlab.com:owner/repo.git") is None

    def test_non_github_https(self):
        assert parse_github_repo("https://gitlab.com/owner/repo.git") is None

    def test_local_path(self):
        assert parse_github_repo("/tmp/some/repo.git") is None

    def test_file_url(self):
        assert parse_github_repo("file:///tmp/repo.git") is None

    def test_empty_string(self):
        assert parse_github_repo("") is None

    def test_complex_owner_repo(self):
        assert parse_github_repo("git@github.com:my-org/my-repo.git") == ("my-org", "my-repo")

    def test_https_with_dotgit_in_repo_name(self):
        assert parse_github_repo("https://github.com/owner/repo.name.git") == ("owner", "repo.name")


# ---------------------------------------------------------------------------
# _pr_number_from_url
# ---------------------------------------------------------------------------


class TestPrNumberFromUrl:
    def test_standard_url(self):
        assert _pr_number_from_url("https://github.com/owner/repo/pull/42") == 42

    def test_url_with_trailing_newline(self):
        assert _pr_number_from_url("https://github.com/owner/repo/pull/1") == 1

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse PR number"):
            _pr_number_from_url("https://github.com/owner/repo")


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------


class TestDetectProvider:
    def test_github_with_gh_authenticated(self):
        with patch("pubgate.pr._gh_is_available", return_value=True):
            provider = detect_provider("git@github.com:owner/repo.git")
        assert isinstance(provider, GitHubCLIProvider)

    def test_github_gh_not_available_returns_none(self, caplog):
        with patch("pubgate.pr._gh_is_available", return_value=False):
            with caplog.at_level(logging.WARNING):
                provider = detect_provider("git@github.com:owner/repo.git")
        assert provider is None
        assert "gh" in caplog.text

    def test_non_github_returns_none(self):
        with patch("pubgate.pr._gh_is_available", return_value=True):
            provider = detect_provider("git@gitlab.com:owner/repo.git")
        assert provider is None

    def test_local_path_returns_none(self):
        with patch("pubgate.pr._gh_is_available", return_value=True):
            provider = detect_provider("/tmp/some/repo.git")
        assert provider is None


# ---------------------------------------------------------------------------
# GitHubCLIProvider
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class TestGitHubCLIProvider:
    def _make_provider(self):
        return GitHubCLIProvider("owner", "repo")

    @patch("pubgate.pr.subprocess.run")
    def test_creates_new_pr(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([])),  # pr list: no existing PRs
            _completed("https://github.com/owner/repo/pull/1\n"),  # pr create: outputs URL
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Test PR", body="body")

        assert result == PRResult(url="https://github.com/owner/repo/pull/1", number=1, created=True)
        assert mock_run.call_count == 2

        # Verify create command does not use --json
        create_cmd = mock_run.call_args_list[1][0][0]
        assert "create" in create_cmd
        assert "--json" not in create_cmd
        assert "--repo" in create_cmd
        idx = create_cmd.index("--repo")
        assert create_cmd[idx + 1] == "owner/repo"

    @patch("pubgate.pr.subprocess.run")
    def test_updates_existing_pr(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"number": 42, "url": "https://github.com/owner/repo/pull/42"}])),  # pr list
            _completed(""),  # gh api PATCH
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Updated", body="new body")

        assert result == PRResult(url="https://github.com/owner/repo/pull/42", number=42, created=False)

        # Verify REST API call instead of gh pr edit
        api_cmd = mock_run.call_args_list[1][0][0]
        assert "api" in api_cmd
        assert "--method" in api_cmd
        assert "PATCH" in api_cmd
        assert "/repos/owner/repo/pulls/42" in api_cmd

    @patch("pubgate.pr.subprocess.run")
    def test_update_pr_api_failure_still_returns_result(self, mock_run):
        """If gh api PATCH fails, still return the PR info."""
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"number": 17, "url": "https://github.com/owner/repo/pull/17"}])),  # pr list
            _completed(stderr="API error", rc=1),  # gh api PATCH fails
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Updated", body="new body")

        assert result == PRResult(url="https://github.com/owner/repo/pull/17", number=17, created=False)

    @patch("pubgate.pr.subprocess.run")
    def test_gh_error_propagates(self, mock_run):
        provider = self._make_provider()
        mock_run.return_value = _completed(stderr="not authenticated", rc=1)

        with pytest.raises(RuntimeError, match="failed"):
            provider.create_or_update_pr(head="h", base="b", title="t", body="")


# ---------------------------------------------------------------------------
# Integration: _handle_pr fallback on provider error
# ---------------------------------------------------------------------------


class TestHandlePrProviderError:
    def test_provider_exception_falls_back_to_manual_steps(self, topo: Topology, caplog):
        """When gh pr create/update raises, _handle_pr logs a warning and falls back."""
        topo.stage_and_merge()
        # Make the remote look like GitHub so detect_provider returns a provider
        with (
            patch("pubgate.core.detect_provider") as mock_detect,
            caplog.at_level(logging.INFO),
        ):
            mock_provider = mock_detect.return_value
            mock_provider.create_or_update_pr.side_effect = RuntimeError("gh network timeout")

            topo.pubgate.publish()

        assert "Automatic PR creation failed" in caplog.text
        assert "gh network timeout" in caplog.text
        assert "Create PR" in caplog.text
        assert "Next steps:" in caplog.text


# ---------------------------------------------------------------------------
# Integration: dry-run reflects provider availability
# ---------------------------------------------------------------------------


class TestDryRunPrMessages:
    def test_dry_run_with_provider_shows_automatic(self, topo: Topology, caplog):
        """dry-run should say 'Would create/update PR automatically' when gh is available."""
        topo.stage_and_merge()
        with (
            patch("pubgate.core.detect_provider") as mock_detect,
            caplog.at_level(logging.INFO),
        ):
            mock_detect.return_value = object()  # non-None → provider available
            topo.pubgate.publish(dry_run=True)

        assert "Would create/update PR" in caplog.text
        assert "Review and merge the PR" in caplog.text
        assert "Next steps:" in caplog.text
        assert "Create PR" not in caplog.text

    def test_dry_run_without_provider_shows_manual(self, topo: Topology, caplog):
        """dry-run should fall back to manual steps when no provider is available."""
        topo.stage_and_merge()
        with caplog.at_level(logging.INFO):
            topo.pubgate.publish(dry_run=True)

        assert "Next steps:" in caplog.text
        assert "Create PR" in caplog.text


# ---------------------------------------------------------------------------
# Integration: --no-pr and non-GitHub fallback
# ---------------------------------------------------------------------------


class TestNoPrFlag:
    def test_no_pr_shows_manual_steps(self, topo: Topology, caplog):
        topo.stage_and_merge()
        with caplog.at_level(logging.INFO):
            topo.pubgate.publish(no_pr=True)
        assert "Create PR" in caplog.text
        assert "Next steps:" in caplog.text

    def test_non_github_remote_shows_manual_steps(self, topo: Topology, caplog):
        # topo uses local file paths as remotes, which are not GitHub
        topo.stage_and_merge()
        with caplog.at_level(logging.INFO):
            topo.pubgate.publish()
        assert "Create PR" in caplog.text
        assert "Next steps:" in caplog.text

    def test_absorb_no_pr_shows_manual_steps(self, topo: Topology, caplog):
        with caplog.at_level(logging.INFO):
            topo.pubgate.absorb(no_pr=True)
        assert "Create PR" in caplog.text

    def test_stage_no_pr_shows_manual_steps(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        with caplog.at_level(logging.INFO):
            topo.pubgate.stage(no_pr=True)
        assert "Create PR" in caplog.text


class TestNoPrCLIFlag:
    def test_parser_accepts_no_pr(self):
        from pubgate.__main__ import build_parser

        parser = build_parser()
        args = parser.parse_args(["publish", "--no-pr"])
        assert args.no_pr is True

    def test_parser_default_no_pr_is_false(self):
        from pubgate.__main__ import build_parser

        parser = build_parser()
        args = parser.parse_args(["absorb"])
        assert args.no_pr is False

    def test_no_pr_works_on_all_commands(self):
        from pubgate.__main__ import build_parser

        parser = build_parser()
        for cmd in ("absorb", "stage", "publish"):
            args = parser.parse_args([cmd, "--no-pr"])
            assert args.no_pr is True, f"--no-pr not accepted for {cmd}"
