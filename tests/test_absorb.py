import logging

import pytest
from conftest import SAMPLE_LATIN1, SAMPLE_PNG, Topology

from pubgate.errors import PubGateError
from pubgate.git import GitRepo


class TestAbsorbEmptyPublicRepo:
    def test_errors_on_empty_public_repo(self, topo_empty_public: Topology):
        with pytest.raises(PubGateError, match="no 'main' branch"):
            topo_empty_public.pubgate.absorb()


class TestAbsorbBootstrap:
    def test_creates_tracking_branch(self, topo: Topology):
        topo.bootstrap_absorb()
        state = topo.work_dir.read_file_at_ref("main", topo.cfg.inbound_state_file)
        assert state is not None
        public_head = topo.work_dir.git.rev_parse(f"{topo.cfg.public_remote}/main")
        assert state.strip() == public_head

    def test_dry_run_skips_branch(self, topo: Topology):
        topo.pubgate.absorb(dry_run=True)
        assert not topo.work_dir.git.branch_exists(topo.cfg.inbound_pr_branch)

    def test_warns_no_prior_publish(self, topo: Topology, caplog):
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "no prior publish" in caplog.text.lower()


class TestAbsorbChanges:
    def test_no_op(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "Already up to date" in caplog.text

    def test_new_file(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"new-file.txt": "new content\n"})
        topo.pubgate.absorb()
        content = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "new-file.txt")
        assert content is not None
        assert "new content" in content

    def test_new_file_preserves_local_when_exists(self, topo: Topology, caplog):
        """File added on public that already exists locally is kept, not overwritten."""
        topo.bootstrap_absorb()
        # Add a file to internal that doesn't exist on public yet
        topo.commit_internal({"shared.txt": "internal version with secrets\n"})
        # Add the same file on the public side
        topo.commit_to_public({"shared.txt": "public version\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "kept local version" in caplog.text
        # Local version must be preserved
        content = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "shared.txt")
        assert content is not None
        assert "internal version with secrets" in content
        assert "public version" not in content

    def test_modified_file(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"public-file.txt": "updated content\n"})
        topo.pubgate.absorb()
        content = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "public-file.txt")
        assert content is not None
        assert "updated content" in content

    def test_deleted_file_kept_locally(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # Absorb a file, then delete it on the public repo
        topo.commit_to_public({"deleteme.txt": "will be deleted\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")
        topo.commit_to_public(delete=["deleteme.txt"])
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "deleted on public" in caplog.text
        assert "deleteme.txt" in topo.work_dir.list_files_at_ref(topo.cfg.inbound_pr_branch)

    def test_multiple_changes(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"public-file.txt": "updated\n", "extra.txt": "extra\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")
        assert topo.work_dir.read_file_at_ref("main", "extra.txt") is not None
        assert "updated" in (topo.work_dir.read_file_at_ref("main", "public-file.txt") or "")

    def test_rename(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.rename_on_public("public-file.txt", "renamed-file.txt", "rename file")
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "rename" in caplog.text.lower()
        assert topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "renamed-file.txt") is not None


class TestAbsorbMerge:
    def test_clean_merge(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.setup_baseline("public-file.txt", "line1\nline2\nline3\nline4\nline5\n")
        topo.commit_internal({"public-file.txt": "INTERNAL\nline2\nline3\nline4\nline5\n"})
        topo.commit_to_public({"public-file.txt": "line1\nline2\nline3\nline4\nPUBLIC\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "merge (clean)" in caplog.text

        merged = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "public-file.txt")
        assert merged is not None
        lines = merged.splitlines()
        assert lines[0] == "INTERNAL"
        assert lines[1:4] == ["line2", "line3", "line4"]
        assert lines[4] == "PUBLIC"

    def test_conflict(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.setup_baseline("public-file.txt", "original line\n")
        topo.commit_internal({"public-file.txt": "internal version\n"})
        topo.commit_to_public({"public-file.txt": "public version\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "CONFLICTS" in caplog.text

        conflicted = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "public-file.txt")
        assert conflicted is not None
        assert "<<<<<<<" in conflicted
        assert "=======" in conflicted
        assert ">>>>>>>" in conflicted
        assert "internal version" in conflicted
        assert "public version" in conflicted

        # Commit message should mention the conflicted file
        commit_msg = topo.work_dir.run("log", "-1", "--format=%B", topo.cfg.inbound_pr_branch).strip()
        assert "CONFLICTS" in commit_msg
        assert "public-file.txt" in commit_msg

    def test_sequential_merges_use_correct_base(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.setup_baseline("public-file.txt", "line1\nline2\nline3\n")

        # Round 1: public changes line 3
        topo.commit_to_public({"public-file.txt": "line1\nline2\nPUBLIC-V1\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Internal changes line 1 (file now has both changes on main)
        topo.commit_internal({"public-file.txt": "INTERNAL-V2\nline2\nPUBLIC-V1\n"}, push=True)

        # Round 2: public changes line 3 again
        topo.commit_to_public({"public-file.txt": "line1\nline2\nPUBLIC-V2\n"})
        topo.pubgate.absorb()

        # The base for this merge is the round-1 public version (line1/line2/PUBLIC-V1).
        # Internal changed line 1 → INTERNAL-V2, public changed line 3 → PUBLIC-V2.
        # If base were wrong, internal's line-1 change would conflict.
        merged = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "public-file.txt")
        assert merged is not None
        lines = merged.splitlines()
        assert lines[0] == "INTERNAL-V2"
        assert lines[1] == "line2"
        assert lines[2] == "PUBLIC-V2"


class TestAbsorbEdgeCases:
    def test_dry_run(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_to_public({"new.txt": "new\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb(dry_run=True)
        assert "[dry-run]" in caplog.text
        assert "new.txt" in caplog.text

    def test_dirty_worktree_aborts(self, topo: Topology):
        (topo.work_dir.path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        topo.work_dir.run("add", "dirty.txt")
        with pytest.raises(PubGateError, match="not clean"):
            topo.pubgate.absorb()


class TestAbsorbBinary:
    def test_binary_file_added(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"logo.png": SAMPLE_PNG})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.inbound_pr_branch, "logo.png")
        assert absorbed == SAMPLE_PNG

    def test_non_utf8_file_absorbed_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"legacy.txt": SAMPLE_LATIN1})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.inbound_pr_branch, "legacy.txt")
        assert absorbed == SAMPLE_LATIN1

    def test_binary_file_modified(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        binary_v1 = b"\x00\x01\x02\x03"
        binary_v2 = b"\x00\x04\x05\x06"
        topo.commit_to_public({"data.bin": binary_v1})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")
        topo.commit_to_public({"data.bin": binary_v2})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "binary" in caplog.text.lower()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.inbound_pr_branch, "data.bin")
        assert absorbed == binary_v2


class TestAbsorbStateValidation:
    def test_garbage_state_file_aborts(self, topo: Topology):
        # Write garbage to the inbound state file on main
        topo.commit_internal({topo.cfg.inbound_state_file: "not-a-valid-sha\n"}, push=True)

        with pytest.raises(PubGateError, match="invalid SHA"):
            topo.pubgate.absorb()


class TestMergeFileValidation:
    def test_unreadable_modified_file_raises(self, topo: Topology):
        from unittest.mock import patch

        from pubgate.errors import PubGateError

        # Push a file to public BEFORE bootstrap so it's part of the baseline
        topo.commit_to_public({"tracked.txt": "original\n"})

        topo.bootstrap_absorb()

        # Modify the file on public so diff_tree reports 'M'
        topo.commit_to_public({"tracked.txt": "modified\n"})

        # We also need the file on internal main so _merge_file takes the
        # three-way merge path (not the "missing locally" shortcut)
        topo.commit_internal({"tracked.txt": "original\n"}, push=True)

        # Patch read_file_at_ref to return None for tracked.txt only
        original_read = GitRepo.read_file_at_ref

        def fake_read(self, ref, path):
            if path == "tracked.txt":
                return None
            return original_read(self, ref, path)

        with patch.object(GitRepo, "read_file_at_ref", fake_read):
            with pytest.raises(PubGateError, match="unreadable"):
                topo.pubgate.absorb()


class TestAbsorbMixedChanges:
    def test_mixed_binary_and_text_in_single_absorb(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"readme.txt": "hello world\n", "icon.png": SAMPLE_PNG}, "mixed text and binary")
        topo.pubgate.absorb()

        content = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "readme.txt")
        assert content is not None
        assert "hello world" in content

        absorbed_bin = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.inbound_pr_branch, "icon.png")
        assert absorbed_bin == SAMPLE_PNG

    def test_text_file_becomes_binary(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # First, push a text file
        topo.commit_to_public({"data.dat": "text content\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Now replace with binary content
        binary_data = b"\x00\x01\x02\x03\x04\x05"
        topo.commit_to_public({"data.dat": binary_data})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.inbound_pr_branch, "data.dat")
        assert absorbed == binary_data

    def test_delete_and_add_simultaneous(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"old-file.txt": "will be deleted\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Delete old file, add new file in one commit
        topo.commit_to_public({"new-file.txt": "fresh content\n"}, "delete old, add new", delete=["old-file.txt"])
        topo.pubgate.absorb()

        files = topo.work_dir.list_files_at_ref(topo.cfg.inbound_pr_branch)
        assert "new-file.txt" in files
        # Deleted files are kept locally (by design)
        assert "old-file.txt" in files
