import json
import logging
import subprocess
from unittest.mock import patch

import pytest
from conftest import Topology

from pubgate.pr import (
    AzureDevOpsCLIProvider,
    GitHubCLIProvider,
    PRResult,
    _az_devops_is_available,
    _gh_is_available,
    _pr_number_from_url,
    detect_provider,
    parse_azure_devops_repo,
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


# ---------------------------------------------------------------------------
# _gh_is_available: direct tests
# ---------------------------------------------------------------------------


class TestGhIsAvailable:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _gh_is_available.cache_clear()
        yield
        _gh_is_available.cache_clear()

    @patch("pubgate.pr.subprocess.run")
    def test_returns_true_when_authenticated(self, mock_run):
        mock_run.return_value = _completed()
        assert _gh_is_available() is True

    @patch("pubgate.pr.subprocess.run")
    def test_returns_false_when_not_authenticated(self, mock_run):
        mock_run.return_value = _completed(rc=1, stderr="not logged in")
        assert _gh_is_available() is False

    @patch("pubgate.pr.subprocess.run", side_effect=FileNotFoundError("gh not found"))
    def test_returns_false_when_gh_not_installed(self, _mock):
        assert _gh_is_available() is False

    @patch("pubgate.pr.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30))
    def test_returns_false_on_timeout(self, _mock):
        assert _gh_is_available() is False

    @patch("pubgate.pr.subprocess.run")
    def test_result_is_cached(self, mock_run):
        mock_run.return_value = _completed()
        assert _gh_is_available() is True
        assert _gh_is_available() is True
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# GitHubCLIProvider: additional edge cases
# ---------------------------------------------------------------------------


class TestGitHubCLIProviderEdgeCases:
    def _make_provider(self):
        return GitHubCLIProvider("owner", "repo")

    @patch("pubgate.pr.subprocess.run")
    def test_update_pr_api_failure_logs_warning(self, mock_run, caplog):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"number": 5, "url": "https://github.com/owner/repo/pull/5"}])),
            _completed(stderr="Server error", rc=1),
        ]

        with caplog.at_level(logging.WARNING):
            result = provider.create_or_update_pr(head="f", base="m", title="T", body="B")

        assert result.created is False
        assert result.number == 5
        assert "Could not update PR title/body" in caplog.text

    @patch("pubgate.pr.subprocess.run")
    def test_create_pr_multiline_output(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([])),
            _completed("\nWarning: something\nhttps://github.com/owner/repo/pull/99\n"),
        ]

        result = provider.create_or_update_pr(head="f", base="m", title="T", body="B")
        # _pr_number_from_url searches within the string, so it finds the URL
        assert result.number == 99

    @patch("pubgate.pr.subprocess.run")
    def test_find_open_pr_empty_json_array(self, mock_run):
        provider = self._make_provider()
        mock_run.return_value = _completed(json.dumps([]))

        # Access private method directly for coverage
        assert provider._find_open_pr(head="feature", base="main") is None


# ---------------------------------------------------------------------------
# _handle_pr: remote URL exception fallback
# ---------------------------------------------------------------------------


class TestHandlePrRemoteUrlFallback:
    def test_get_remote_url_failure_falls_back(self, topo: Topology, caplog):
        from pubgate.git import GitRepo

        topo.bootstrap_absorb()
        topo.commit_to_public({"new.txt": "content\n"})

        with (
            patch.object(GitRepo, "get_remote_url", side_effect=RuntimeError("no such remote")),
            caplog.at_level(logging.INFO),
        ):
            topo.pubgate.absorb()

        assert "Create PR" in caplog.text
        assert "Next steps:" in caplog.text


# ---------------------------------------------------------------------------
# parse_azure_devops_repo
# ---------------------------------------------------------------------------


class TestParseAzureDevopsRepo:
    def test_https_url(self):
        assert parse_azure_devops_repo("https://dev.azure.com/myorg/myproject/_git/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_url_with_dot_git(self):
        assert parse_azure_devops_repo("https://dev.azure.com/myorg/myproject/_git/myrepo.git") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_url_trailing_slash(self):
        assert parse_azure_devops_repo("https://dev.azure.com/myorg/myproject/_git/myrepo/") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_http_url(self):
        assert parse_azure_devops_repo("http://dev.azure.com/myorg/myproject/_git/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_url_with_username_prefix(self):
        assert parse_azure_devops_repo("https://myorg@dev.azure.com/myorg/myproject/_git/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_ssh_modern_url(self):
        assert parse_azure_devops_repo("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_ssh_modern_url_with_dot_git(self):
        assert parse_azure_devops_repo("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo.git") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_ssh_legacy_url(self):
        assert parse_azure_devops_repo("user@vs-ssh.visualstudio.com:v3/myorg/myproject/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_ssh_legacy_url_with_dot_git(self):
        assert parse_azure_devops_repo("user@vs-ssh.visualstudio.com:v3/myorg/myproject/myrepo.git") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_legacy_visualstudio_url(self):
        assert parse_azure_devops_repo("https://myorg.visualstudio.com/myproject/_git/myrepo") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_legacy_visualstudio_url_with_dot_git(self):
        assert parse_azure_devops_repo("https://myorg.visualstudio.com/myproject/_git/myrepo.git") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_https_legacy_visualstudio_url_trailing_slash(self):
        assert parse_azure_devops_repo("https://myorg.visualstudio.com/myproject/_git/myrepo/") == (
            "myorg",
            "myproject",
            "myrepo",
        )

    def test_complex_names(self):
        assert parse_azure_devops_repo("https://dev.azure.com/my-org/my-project/_git/my-repo.name") == (
            "my-org",
            "my-project",
            "my-repo.name",
        )

    def test_github_url_returns_none(self):
        assert parse_azure_devops_repo("git@github.com:owner/repo.git") is None

    def test_gitlab_url_returns_none(self):
        assert parse_azure_devops_repo("https://gitlab.com/owner/repo.git") is None

    def test_local_path_returns_none(self):
        assert parse_azure_devops_repo("/tmp/some/repo.git") is None

    def test_empty_string_returns_none(self):
        assert parse_azure_devops_repo("") is None


# ---------------------------------------------------------------------------
# detect_provider: Azure DevOps
# ---------------------------------------------------------------------------


class TestDetectProviderAzureDevops:
    def test_azure_devops_with_az_authenticated(self):
        with patch("pubgate.pr._az_devops_is_available", return_value=True):
            provider = detect_provider("https://dev.azure.com/myorg/myproject/_git/myrepo")
        assert isinstance(provider, AzureDevOpsCLIProvider)

    def test_azure_devops_az_not_available_returns_none(self, caplog):
        with patch("pubgate.pr._az_devops_is_available", return_value=False):
            with caplog.at_level(logging.WARNING):
                provider = detect_provider("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo")
        assert provider is None
        assert "az" in caplog.text

    def test_github_url_still_returns_github_provider(self):
        with patch("pubgate.pr._gh_is_available", return_value=True):
            provider = detect_provider("git@github.com:owner/repo.git")
        assert isinstance(provider, GitHubCLIProvider)

    def test_non_matching_url_returns_none(self):
        provider = detect_provider("/tmp/some/repo.git")
        assert provider is None


# ---------------------------------------------------------------------------
# AzureDevOpsCLIProvider
# ---------------------------------------------------------------------------


class TestAzureDevOpsCLIProvider:
    def _make_provider(self):
        return AzureDevOpsCLIProvider("myorg", "myproject", "myrepo")

    @patch("pubgate.pr.subprocess.run")
    def test_creates_new_pr(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([])),  # pr list: no existing PRs
            _completed(json.dumps({"pullRequestId": 1})),  # pr create: returns JSON
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Test PR", body="body")

        assert result == PRResult(
            url="https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/1", number=1, created=True
        )
        assert mock_run.call_count == 2

        # Verify create command uses --description not --body
        create_cmd = mock_run.call_args_list[1][0][0]
        assert "create" in create_cmd
        assert "--description" in create_cmd
        assert "--body" not in create_cmd
        assert "--detect" in create_cmd
        idx = create_cmd.index("--detect")
        assert create_cmd[idx + 1] == "false"

    @patch("pubgate.pr.subprocess.run")
    def test_updates_existing_pr(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"pullRequestId": 42}])),  # pr list
            _completed(json.dumps({"pullRequestId": 42})),  # pr update
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Updated", body="new body")

        assert result == PRResult(
            url="https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/42", number=42, created=False
        )

        # Verify update command uses --id
        update_cmd = mock_run.call_args_list[1][0][0]
        assert "update" in update_cmd
        assert "--id" in update_cmd
        idx = update_cmd.index("--id")
        assert update_cmd[idx + 1] == "42"

    @patch("pubgate.pr.subprocess.run")
    def test_update_pr_failure_still_returns_result(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"pullRequestId": 17}])),  # pr list
            _completed(stderr="API error", rc=1),  # pr update fails
        ]

        result = provider.create_or_update_pr(head="feature", base="main", title="Updated", body="new body")

        assert result == PRResult(
            url="https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/17", number=17, created=False
        )

    @patch("pubgate.pr.subprocess.run")
    def test_az_error_propagates(self, mock_run):
        provider = self._make_provider()
        mock_run.return_value = _completed(stderr="not authenticated", rc=1)

        with pytest.raises(RuntimeError, match="failed"):
            provider.create_or_update_pr(head="h", base="b", title="t", body="")

    @patch("pubgate.pr.subprocess.run")
    def test_update_pr_failure_logs_warning(self, mock_run, caplog):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([{"pullRequestId": 5}])),
            _completed(stderr="Server error", rc=1),
        ]

        with caplog.at_level(logging.WARNING):
            result = provider.create_or_update_pr(head="f", base="m", title="T", body="B")

        assert result.created is False
        assert result.number == 5
        assert "Could not update PR title/body" in caplog.text

    @patch("pubgate.pr.subprocess.run")
    def test_find_open_pr_empty_json_array(self, mock_run):
        provider = self._make_provider()
        mock_run.return_value = _completed(json.dumps([]))

        assert provider._find_open_pr(head="feature", base="main") is None

    @patch("pubgate.pr.subprocess.run")
    def test_pr_url_construction(self, mock_run):
        provider = self._make_provider()

        mock_run.side_effect = [
            _completed(json.dumps([])),
            _completed(json.dumps({"pullRequestId": 123})),
        ]

        result = provider.create_or_update_pr(head="f", base="m", title="T", body="B")
        assert result.url == "https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/123"


# ---------------------------------------------------------------------------
# _az_devops_is_available: direct tests
# ---------------------------------------------------------------------------


class TestAzDevopsIsAvailable:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _az_devops_is_available.cache_clear()
        yield
        _az_devops_is_available.cache_clear()

    @patch("pubgate.pr.subprocess.run")
    def test_returns_true_when_authenticated(self, mock_run):
        mock_run.return_value = _completed()
        assert _az_devops_is_available() is True
        # Called twice: az account show + az extension add
        assert mock_run.call_count == 2

    @patch("pubgate.pr.subprocess.run")
    def test_returns_false_when_not_authenticated(self, mock_run):
        mock_run.return_value = _completed(rc=1, stderr="not logged in")
        assert _az_devops_is_available() is False

    @patch("pubgate.pr.subprocess.run", side_effect=FileNotFoundError("az not found"))
    def test_returns_false_when_az_not_installed(self, _mock):
        assert _az_devops_is_available() is False

    @patch("pubgate.pr.subprocess.run", side_effect=subprocess.TimeoutExpired("az", 30))
    def test_returns_false_on_timeout(self, _mock):
        assert _az_devops_is_available() is False

    @patch("pubgate.pr.subprocess.run")
    def test_result_is_cached(self, mock_run):
        mock_run.return_value = _completed()
        assert _az_devops_is_available() is True
        assert _az_devops_is_available() is True
        # 2 calls on first invocation (account show + extension add), none on second (cached)
        assert mock_run.call_count == 2
