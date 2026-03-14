from pathlib import Path

import pytest
from conftest import Topology

from pubgate.__main__ import main
from pubgate.config import Config
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
        from pubgate.config import load_config

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
            'internal_main_branch = "main"\ninternal_preview_branch = "main"\n',
            encoding="utf-8",
        )
        from pubgate.config import load_config

        with pytest.raises(PubGateError, match="share the same branch name"):
            load_config(tmp_path)

    def test_distinct_branch_names_pass(self):
        from pubgate.config import Config

        # Default config has all distinct branch names
        cfg = Config()
        assert cfg.internal_main_branch != cfg.internal_preview_branch


class TestStateFileNameCollision:
    def test_same_state_filenames_rejected(self, tmp_path: Path):
        config_path = tmp_path / "pubgate.toml"
        config_path.write_text(
            'public_main_branch = "pub-main"\n'
            'internal_main_branch = "int-main"\n'
            'inbound_state_file = ".sync-state"\n'
            'outbound_state_file = ".sync-state"\n',
            encoding="utf-8",
        )
        from pubgate.config import load_config

        with pytest.raises(PubGateError, match="share the same filename"):
            load_config(tmp_path)

    def test_distinct_state_filenames_pass(self):
        from pubgate.config import Config

        cfg = Config()
        assert cfg.inbound_state_file != cfg.outbound_state_file


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
        from pubgate.config import load_config

        with pytest.raises(PubGateError, match="No pubgate.toml found"):
            load_config(tmp_path)

    def test_unknown_key_rejected(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text('bogus_key = "value"\n', encoding="utf-8")
        from pubgate.config import load_config

        with pytest.raises(PubGateError, match="Unknown keys"):
            load_config(tmp_path)


class TestRepoDirFlag:
    def test_repo_dir_parsed(self):
        from pubgate.__main__ import build_parser

        args = build_parser().parse_args(["--repo-dir", "/some/path", "absorb"])
        assert args.repo_dir == "/some/path"
        assert args.command == "absorb"

    def test_repo_dir_default(self):
        from pubgate.__main__ import build_parser

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
