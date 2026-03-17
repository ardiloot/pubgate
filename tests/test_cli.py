import logging
import subprocess
from pathlib import Path

import pytest
from conftest import Topology

from pubgate.__main__ import build_parser, main
from pubgate.config import Config, load_config
from pubgate.errors import GitError, PubGateError
from pubgate.git import GitRepo
from pubgate.state import validate_state_sha


class TestCLI:
    def test_help_output(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert "pubgate" in capsys.readouterr().out

    def test_no_command_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 1

    def test_unknown_command_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main(["bogus"])
        assert exc.value.code != 0


class TestRepoInitCheck:
    def test_non_git_directory(self, tmp_path: Path):
        git = GitRepo(tmp_path)
        with pytest.raises(PubGateError, match="not a git repository"):
            git.verify_repo()


class TestBranchNameValidation:
    def test_valid_branch_names(self):
        # Config() triggers __post_init__ which validates all branch names;
        # no exception means all defaults are valid.
        Config()

    def test_invalid_branch_name(self, tmp_path: Path):
        config_path = tmp_path / "pubgate.toml"
        config_path.write_text(
            'internal_main_branch = "my branch"\n',
            encoding="utf-8",
        )

        with pytest.raises(PubGateError, match="invalid characters"):
            load_config(tmp_path)


class TestSubprocessTimeout:
    def test_timeout_raises_git_error(self, topo: Topology):
        # Use a 0-second timeout to guarantee expiration
        with pytest.raises(GitError, match="timed out"):
            topo.work_dir.git._run("version", timeout=0)


class TestStateShaValidation:
    def test_valid_sha_passes(self):
        sha = "  a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2  \n"
        result = validate_state_sha(sha, "test-label")
        assert result == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    def test_garbage_sha_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("not-a-sha", "test-label")

    def test_short_hex_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("a1b2c3d4", "test-label")

    def test_too_long_hex_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("a" * 41, "test-label")

    def test_empty_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("", "test-label")

    def test_uppercase_hex_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("A" * 40, "test-label")

    def test_whitespace_only_rejected(self):
        with pytest.raises(PubGateError, match="invalid SHA"):
            validate_state_sha("   \n\t  ", "test-label")


class TestBranchNameCollision:
    def test_duplicate_branch_names_rejected(self, tmp_path: Path):
        config_path = tmp_path / "pubgate.toml"
        config_path.write_text(
            'internal_main_branch = "main"\ninternal_approved_branch = "main"\n',
            encoding="utf-8",
        )

        with pytest.raises(PubGateError, match="share the same branch name"):
            load_config(tmp_path)

    def test_distinct_branch_names_pass(self):
        # Default config has all distinct branch names
        cfg = Config()
        assert cfg.internal_main_branch != cfg.internal_approved_branch


class TestStateFileNameCollision:
    def test_same_state_filenames_rejected(self, tmp_path: Path):
        config_path = tmp_path / "pubgate.toml"
        config_path.write_text(
            'public_main_branch = "pub-main"\n'
            'internal_main_branch = "int-main"\n'
            'absorb_state_file = ".sync-state"\n'
            'stage_state_file = ".sync-state"\n',
            encoding="utf-8",
        )

        with pytest.raises(PubGateError, match="share the same filename"):
            load_config(tmp_path)

    def test_distinct_state_filenames_pass(self):
        cfg = Config()
        assert cfg.absorb_state_file != cfg.stage_state_file


class TestDetachedHead:
    def test_detached_head_aborts(self, topo: Topology):
        # Detach HEAD
        sha = topo.work_dir.run("rev-parse", "HEAD").strip()
        topo.work_dir.run("checkout", sha)

        with pytest.raises(PubGateError, match="detached HEAD"):
            with topo.work_dir.git.on_branch("main"):
                pass  # should not reach here

        # Restore for cleanup
        topo.work_dir.run("checkout", "main")


class TestReadFileAtRefErrors:
    def test_missing_file_returns_none(self, topo: Topology):
        assert topo.work_dir.git.read_file_at_ref("HEAD", "no-such-file.txt") is None

    def test_missing_file_bytes_returns_none(self, topo: Topology):
        assert topo.work_dir.git.read_file_at_ref_bytes("HEAD", "no-such-file.txt") is None

    def test_bad_ref_returns_none(self, topo: Topology):
        assert topo.work_dir.git.read_file_at_ref("invalid-ref-zzz", "file1.txt") is None


class TestMissingConfig:
    def test_missing_pubgate_toml_raises(self, tmp_path: Path):
        with pytest.raises(PubGateError, match="No pubgate.toml found"):
            load_config(tmp_path)

    def test_unknown_key_rejected(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text('bogus_key = "value"\n', encoding="utf-8")

        with pytest.raises(PubGateError, match="Unknown keys"):
            load_config(tmp_path)


class TestRepoDirFlag:
    def test_repo_dir_parsed(self):
        args = build_parser().parse_args(["--repo-dir", "/some/path", "absorb"])
        assert args.repo_dir == "/some/path"
        assert args.command == "absorb"

    def test_repo_dir_default(self):
        args = build_parser().parse_args(["absorb"])
        assert args.repo_dir == "."


class TestConfigFieldMetadata:
    def test_all_config_fields_have_kind_metadata(self):
        from dataclasses import fields as dc_fields

        for f in dc_fields(Config):
            if f.name == "repo_dir":
                continue
            assert "kind" in f.metadata, f"Config field '{f.name}' is missing 'kind' metadata"

    def test_all_branch_fields_have_scope(self):
        from dataclasses import fields as dc_fields

        for f in dc_fields(Config):
            if f.metadata.get("kind") == "branch":
                assert "scope" in f.metadata, f"Branch field '{f.name}' is missing 'scope' metadata"


class TestMalformedToml:
    def test_invalid_toml_raises(self, tmp_path: Path):
        import sys

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # type: ignore[import-untyped]
        (tmp_path / "pubgate.toml").write_text("key =\n", encoding="utf-8")

        with pytest.raises((PubGateError, tomllib.TOMLDecodeError)):
            load_config(tmp_path)


class TestConfigTypeValidation:
    def test_non_string_for_string_field(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text("public_url = 123\n", encoding="utf-8")

        with pytest.raises(PubGateError, match="must be a string"):
            load_config(tmp_path)

    def test_mixed_type_list_in_ignore(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text('ignore = ["*.txt", 123]\n', encoding="utf-8")

        with pytest.raises(PubGateError, match="must be a list of strings"):
            load_config(tmp_path)

    def test_non_list_for_ignore(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text('ignore = "not-a-list"\n', encoding="utf-8")

        with pytest.raises(PubGateError, match="must be a list of strings"):
            load_config(tmp_path)


class TestEnsureRemote:
    def test_existing_remote_url_mismatch_updates(self, topo: Topology):
        old_url = topo.work_dir.git.get_remote_url("public-remote")
        new_url = "/tmp/fake-url.git"
        topo.work_dir.git.ensure_remote("public-remote", new_url)
        assert topo.work_dir.git.get_remote_url("public-remote") == new_url
        # Restore
        topo.work_dir.git.ensure_remote("public-remote", old_url)

    def test_missing_remote_without_url_raises(self, topo: Topology):
        with pytest.raises(PubGateError, match="does not exist"):
            topo.work_dir.git.ensure_remote("no-such-remote", None)

    def test_missing_remote_with_url_creates(self, topo: Topology):
        topo.work_dir.git.ensure_remote("new-remote", "/tmp/test.git")
        assert topo.work_dir.git.get_remote_url("new-remote") == "/tmp/test.git"


class TestCopyFileFromRefWarning:
    def test_warning_on_unreadable_text_file(self, topo: Topology, caplog):
        from unittest.mock import patch as mock_patch

        git = topo.work_dir.git
        with (
            mock_patch.object(git, "read_file_at_ref_bytes", return_value=None),
            caplog.at_level(logging.WARNING, logger="pubgate"),
        ):
            git.copy_file_from_ref("HEAD", "ghost.txt")
        assert "Could not read file" in caplog.text

    def test_warning_on_unreadable_binary_file(self, topo: Topology, caplog):
        git = topo.work_dir.git
        from unittest.mock import patch as mock_patch

        with (
            mock_patch.object(git, "read_file_at_ref_bytes", return_value=None),
            caplog.at_level(logging.WARNING, logger="pubgate"),
        ):
            git.copy_file_from_ref("HEAD", "ghost.bin")
        assert "Could not read file" in caplog.text


class TestBranchSyncValidation:
    def test_diverged_branches_raise(self, topo: Topology):
        # Create a local-only commit (don't push)
        topo.work_dir.commit_files({"local-only.txt": "local\n"}, "local commit")

        # Create a different commit directly on the server via a temp clone
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            server_url = topo.work_dir.run("remote", "get-url", "origin").strip()
            subprocess.run(["git", "clone", server_url, tmpdir + "/c"], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir + "/c", "checkout", "main"], capture_output=True, check=True)
            (Path(tmpdir + "/c/server-only.txt")).write_text("server\n")
            subprocess.run(["git", "-C", tmpdir + "/c", "add", "."], capture_output=True, check=True)
            subprocess.run(
                ["git", "-C", tmpdir + "/c", "commit", "-m", "server commit"], capture_output=True, check=True
            )
            subprocess.run(["git", "-C", tmpdir + "/c", "push", "origin", "main"], capture_output=True, check=True)

        topo.work_dir.fetch("origin")
        with pytest.raises(PubGateError, match="diverged"):
            topo.work_dir.git.ensure_branch_synced("main", "origin", "main")


class TestFindCommitIntroducing:
    def test_returns_none_when_no_match(self, topo: Topology):
        head = topo.work_dir.git.rev_parse("HEAD")
        topo.work_dir.commit_files({"unrelated.txt": "data\n"}, "unrelated commit")
        new_head = topo.work_dir.git.rev_parse("HEAD")
        result = topo.work_dir.git.find_commit_introducing(head, new_head, "unrelated.txt", "NONEXISTENT_CONTENT")
        assert result is None

    def test_returns_sha_when_found(self, topo: Topology):
        head = topo.work_dir.git.rev_parse("HEAD")
        topo.work_dir.commit_files({"marker.txt": "MARKER_VALUE\n"}, "add marker")
        new_head = topo.work_dir.git.rev_parse("HEAD")
        result = topo.work_dir.git.find_commit_introducing(head, new_head, "marker.txt", "MARKER_VALUE")
        assert result is not None
        assert result == new_head


class TestMainEnsureRemoteFailure:
    def test_main_exits_on_ensure_remote_failure(self, tmp_path: Path):
        # Set up a git repo with a config that references a non-existent remote
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "main"], capture_output=True, check=True)
        (tmp_path / "dummy.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        # Config without public_url and no public-remote configured
        (tmp_path / "pubgate.toml").write_text("", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            main(["--repo-dir", str(tmp_path), "absorb"])
        assert exc.value.code == 1
