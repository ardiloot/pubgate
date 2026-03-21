import logging
from unittest.mock import patch

import pytest
from conftest import Topology

from pubgate.config import DEFAULT_IGNORE_PATTERNS
from pubgate.errors import GitError
from pubgate.filtering import is_ignored
from pubgate.git import is_lfs_pointer
from pubgate.stage_snapshot import build_stage_snapshot, snapshot_unchanged_ref

# ---------------------------------------------------------------------------
# Sample LFS pointer data
# ---------------------------------------------------------------------------

SAMPLE_LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    b"size 12345\n"
)

SAMPLE_LFS_POINTER_STR = SAMPLE_LFS_POINTER.decode("utf-8")


# ---------------------------------------------------------------------------
# is_lfs_pointer unit tests
# ---------------------------------------------------------------------------


class TestIsLfsPointer:
    def test_valid_pointer(self):
        assert is_lfs_pointer(SAMPLE_LFS_POINTER) is True

    def test_valid_pointer_with_extra_fields(self):
        data = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\n"
            b"size 0\n"
        )
        assert is_lfs_pointer(data) is True

    def test_wrong_version(self):
        data = (
            b"version https://git-lfs.github.com/spec/v2\n"
            b"oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
            b"size 12345\n"
        )
        assert is_lfs_pointer(data) is False

    def test_missing_oid(self):
        data = b"version https://git-lfs.github.com/spec/v1\nsize 12345\n"
        assert is_lfs_pointer(data) is False

    def test_missing_size(self):
        data = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
        )
        assert is_lfs_pointer(data) is False

    def test_regular_text(self):
        assert is_lfs_pointer(b"just regular text\n") is False

    def test_empty_bytes(self):
        assert is_lfs_pointer(b"") is False

    def test_binary_data(self):
        assert is_lfs_pointer(b"\x89PNG\r\n\x1a\n\x00\x00") is False

    def test_too_large_to_be_pointer(self):
        data = SAMPLE_LFS_POINTER + b"x" * 512
        assert is_lfs_pointer(data) is False


# ---------------------------------------------------------------------------
# is_binary_at_ref with LFS pointers
# ---------------------------------------------------------------------------


class TestIsBinaryAtRefLfs:
    def test_lfs_pointer_detected_as_binary(self, topo: Topology):
        # Commit an LFS pointer as a regular file (simulates what git stores for LFS files)
        topo.commit_internal({"large.bin": SAMPLE_LFS_POINTER_STR})
        assert topo.work_dir.git.is_binary_at_ref("HEAD", "large.bin")


# ---------------------------------------------------------------------------
# Stage snapshot with LFS pointers
# ---------------------------------------------------------------------------


class TestStageSnapshotLfs:
    def test_lfs_pointer_passes_through_without_scrub(self, topo: Topology):
        topo.commit_internal({"data.bin": SAMPLE_LFS_POINTER_STR})
        snapshot, _ = build_stage_snapshot(
            topo.work_dir.git,
            "HEAD",
            ignore_patterns=[],
            excluded=frozenset(),
        )
        assert "data.bin" in snapshot
        assert snapshot["data.bin"] == SAMPLE_LFS_POINTER_STR

    def test_lfs_pointer_not_scrubbed_for_internal_markers(self, topo: Topology):
        # Even though the pointer text is valid UTF-8, it should not be
        # passed through scrub_internal_blocks
        topo.commit_internal({"model.bin": SAMPLE_LFS_POINTER_STR})
        snapshot, _ = build_stage_snapshot(
            topo.work_dir.git,
            "HEAD",
            ignore_patterns=[],
            excluded=frozenset(),
        )
        # Pointer text should be preserved exactly
        assert snapshot["model.bin"] == SAMPLE_LFS_POINTER_STR


# ---------------------------------------------------------------------------
# .gitattributes not excluded by default ignore patterns
# ---------------------------------------------------------------------------


class TestGitattributesNotIgnored:
    def test_gitattributes_not_matched_by_default_patterns(self):
        assert not is_ignored(".gitattributes", DEFAULT_IGNORE_PATTERNS)

    def test_gitattributes_included_in_snapshot(self, topo: Topology):
        topo.commit_internal({".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"})
        snapshot, _ = build_stage_snapshot(
            topo.work_dir.git,
            "HEAD",
            ignore_patterns=list(DEFAULT_IGNORE_PATTERNS),
            excluded=frozenset(),
        )
        assert ".gitattributes" in snapshot
        attrs = snapshot[".gitattributes"]
        assert isinstance(attrs, str)
        assert "filter=lfs" in attrs


# ---------------------------------------------------------------------------
# is_lfs_available
# ---------------------------------------------------------------------------


class TestIsLfsAvailable:
    def test_result_is_cached(self, topo: Topology):
        git = topo.work_dir.git
        result1 = git.is_lfs_available()
        result2 = git.is_lfs_available()
        assert result1 == result2
        # Verify caching happened
        assert git._lfs_available is not None


# ---------------------------------------------------------------------------
# copy_file_from_ref with LFS pointers
# ---------------------------------------------------------------------------


class TestCopyFileFromRefLfs:
    def test_returns_true_for_lfs_pointer(self, topo: Topology):
        topo.commit_internal({"data.bin": SAMPLE_LFS_POINTER_STR})
        git = topo.work_dir.git
        result = git.copy_file_from_ref("HEAD", "data.bin")
        assert result is True

    def test_preserves_pointer_content(self, topo: Topology):
        topo.commit_internal({"data.bin": SAMPLE_LFS_POINTER_STR})
        git = topo.work_dir.git
        git.copy_file_from_ref("HEAD", "data.bin")
        on_disk = (git.repo_dir / "data.bin").read_text(encoding="utf-8")
        assert on_disk == SAMPLE_LFS_POINTER_STR


# ---------------------------------------------------------------------------
# snapshot_unchanged_ref with LFS pointers
# ---------------------------------------------------------------------------


class TestSnapshotUnchangedLfs:
    def test_identical_lfs_pointer_detected_as_unchanged(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"model.bin": SAMPLE_LFS_POINTER_STR})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.work_dir.run("checkout", "main")

        # Build the same snapshot again (no changes)
        snapshot, _ = build_stage_snapshot(
            topo.work_dir.git,
            topo.cfg.internal_main_branch,
            ignore_patterns=list(topo.cfg.ignore),
            excluded=frozenset({"pubgate.toml"}),
        )
        ref = snapshot_unchanged_ref(topo.cfg, topo.work_dir.git, snapshot)
        assert ref is not None


# ---------------------------------------------------------------------------
# Absorb with LFS pointer files
# ---------------------------------------------------------------------------


class TestAbsorbLfsAdd:
    def test_lfs_pointer_added_on_public_new_file(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_to_public({"model.bin": SAMPLE_LFS_POINTER_STR})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "LFS" in caplog.text
        assert "model.bin" in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "model.bin")
        assert absorbed == SAMPLE_LFS_POINTER_STR

    def test_lfs_pointer_added_on_public_exists_locally(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # File exists locally with different content
        topo.commit_internal({"model.bin": "local version\n"})
        topo.commit_to_public({"model.bin": SAMPLE_LFS_POINTER_STR})
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            topo.pubgate.absorb()
        assert "LFS file" in caplog.text
        assert "kept local version" in caplog.text


class TestAbsorbLfsModify:
    def test_lfs_pointer_modified_on_public(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # Initial LFS pointer on public
        topo.commit_to_public({"model.bin": SAMPLE_LFS_POINTER_STR})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Modified LFS pointer (different sha)
        new_pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "size 99999\n"
        )
        topo.commit_to_public({"model.bin": new_pointer})
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            topo.pubgate.absorb()
        assert "LFS file" in caplog.text
        assert "changed on public" in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "model.bin")
        assert absorbed == new_pointer


# ---------------------------------------------------------------------------
# Stage with LFS pointer logging
# ---------------------------------------------------------------------------


class TestStageLfsLogging:
    def test_stage_logs_lfs_file_count(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_internal({"model.bin": SAMPLE_LFS_POINTER_STR})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert "1 LFS-tracked file" in caplog.text

    def test_stage_logs_multiple_lfs_files(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_internal(
            {
                "a.bin": SAMPLE_LFS_POINTER_STR,
                "b.bin": SAMPLE_LFS_POINTER_STR,
            }
        )
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert "2 LFS-tracked files" in caplog.text


# ---------------------------------------------------------------------------
# Full stage + publish cycle with LFS pointer
# ---------------------------------------------------------------------------


class TestPublishLfs:
    def test_lfs_pointer_survives_full_publish_cycle(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal(
            {
                "model.bin": SAMPLE_LFS_POINTER_STR,
                "readme.txt": "hello\n",
            }
        )
        topo.do_full_publish_cycle()

        # Verify the public repo has the LFS pointer intact
        pub_files = topo.external_contributor.list_files_at_ref("HEAD")
        assert "model.bin" in pub_files
        pub_content = topo.external_contributor.read_file_at_ref("HEAD", "model.bin")
        assert pub_content == SAMPLE_LFS_POINTER_STR

    def test_lfs_pointer_with_gitattributes_in_publish(self, topo: Topology, monkeypatch: pytest.MonkeyPatch):
        # Skip LFS smudge and push; no real LFS object store in tests.
        monkeypatch.setenv("GIT_LFS_SKIP_SMUDGE", "1")
        monkeypatch.setenv("GIT_LFS_SKIP_PUSH", "1")
        topo.bootstrap_absorb()
        topo.commit_internal(
            {
                ".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
                "data.bin": SAMPLE_LFS_POINTER_STR,
                "readme.txt": "hello\n",
            }
        )
        topo.do_full_publish_cycle()

        pub_files = topo.external_contributor.list_files_at_ref("HEAD")
        assert ".gitattributes" in pub_files
        assert "data.bin" in pub_files

        attrs = topo.external_contributor.read_file_at_ref("HEAD", ".gitattributes")
        assert attrs is not None
        assert "filter=lfs" in attrs


# ---------------------------------------------------------------------------
# LFS fetch/push failure handling
# ---------------------------------------------------------------------------


class TestLfsFetchFailure:
    def test_nonzero_exit_logs_warning_and_continues(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = True  # force LFS as available
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            # Fetch from a nonexistent remote ref — should warn, not raise
            git.lfs_fetch("origin", "nonexistent-ref-that-will-fail")
        assert "LFS fetch failed" in caplog.text

    def test_timeout_logs_warning_and_continues(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = True

        original_run = git._run

        def _timeout_run(*args, **kwargs):
            if args and args[0] == "lfs":
                raise GitError(list(args), -1, "timed out after 300s")
            return original_run(*args, **kwargs)

        with patch.object(git, "_run", side_effect=_timeout_run):
            with caplog.at_level(logging.WARNING, logger="pubgate"):
                # Should not raise — timeout is caught and logged
                git.lfs_fetch("origin", "main")
        assert "LFS fetch failed" in caplog.text

    def test_lfs_not_available_skips_silently(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = False
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            git.lfs_fetch("origin", "main")
        assert "LFS fetch failed" not in caplog.text


class TestLfsPushFailure:
    def test_timeout_logs_warning_and_continues(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = True

        original_run = git._run

        def _timeout_run(*args, **kwargs):
            if args and args[0] == "lfs":
                raise GitError(list(args), -1, "timed out after 300s")
            return original_run(*args, **kwargs)

        with patch.object(git, "_run", side_effect=_timeout_run):
            with caplog.at_level(logging.WARNING, logger="pubgate"):
                git.lfs_push("origin", "main")
        assert "LFS push failed" in caplog.text

    def test_nonzero_exit_logs_warning_and_continues(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = True
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            # Push to a nonexistent remote — should warn, not raise
            git.lfs_push("nonexistent-remote", "main")
        assert "LFS push failed" in caplog.text

    def test_lfs_not_available_skips_silently(self, topo: Topology, caplog):
        git = topo.work_dir.git
        git._lfs_available = False
        with caplog.at_level(logging.WARNING, logger="pubgate"):
            git.lfs_push("origin", "main")
        assert "LFS push failed" not in caplog.text
