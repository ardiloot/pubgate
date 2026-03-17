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
        state = topo.work_dir.read_file_at_ref("main", topo.cfg.absorb_state_file)
        assert state is not None
        public_head = topo.work_dir.git.rev_parse(f"{topo.cfg.public_remote}/main")
        assert state.strip() == public_head

    def test_dry_run_skips_branch(self, topo: Topology):
        topo.pubgate.absorb(dry_run=True)
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_absorb_branch)

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
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "new-file.txt")
        assert content is not None
        assert "new content" in content

    def test_new_file_preserves_local_when_exists(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_internal({"shared.txt": "local content\n"})
        topo.commit_to_public({"shared.txt": "public content\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "kept local" in caplog.text
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert content is not None
        assert "local content" in content

    def test_new_file_merge_preserves_internal_blocks(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        internal_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\n"
        topo.commit_internal({"shared.txt": internal_content})
        topo.commit_to_public({"shared.txt": "line1\nline2\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "kept local" in caplog.text
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert content is not None
        assert "BEGIN-INTERNAL" in content
        assert "secret" in content
        assert "line1" in content
        assert "line2" in content

    def test_new_file_merge_integrates_external_changes(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        internal_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\n"
        topo.commit_internal({"shared.txt": internal_content})
        topo.commit_to_public({"shared.txt": "line1\nline2\nexternal addition\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "kept local" in caplog.text
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert content is not None
        assert "BEGIN-INTERNAL" in content
        assert "secret" in content

    def test_modified_file(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"public-file.txt": "updated content\n"})
        topo.pubgate.absorb()
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "public-file.txt")
        assert content is not None
        assert "updated content" in content

    def test_deleted_file_kept_locally(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # Absorb a file, then delete it on the public repo
        topo.commit_to_public({"deleteme.txt": "will be deleted\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")
        topo.commit_to_public(delete=["deleteme.txt"])
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "deleted on public" in caplog.text
        assert "deleteme.txt" in topo.work_dir.list_files_at_ref(topo.cfg.internal_absorb_branch)

    def test_delete_stage_publish_absorb(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # Add a file internally and publish it to public
        topo.commit_internal({"doomed.txt": "will be removed\n"})
        topo.do_full_publish_cycle()
        topo.absorb_and_merge()
        # Delete the file internally, then publish the deletion
        topo.work_dir.delete_files(["doomed.txt"], "remove doomed.txt")
        topo.work_dir.push("origin", "main")
        topo.do_full_publish_cycle()
        # Absorb: file is gone on both sides, should not warn
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "doomed.txt" not in caplog.text
        assert "doomed.txt" not in topo.work_dir.list_files_at_ref(topo.cfg.internal_absorb_branch)

    def test_multiple_changes(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"public-file.txt": "updated\n", "extra.txt": "extra\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")
        assert topo.work_dir.read_file_at_ref("main", "extra.txt") is not None
        assert "updated" in (topo.work_dir.read_file_at_ref("main", "public-file.txt") or "")

    def test_rename(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.rename_on_public("public-file.txt", "renamed-file.txt", "rename file")
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "rename" in caplog.text.lower()
        assert topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "renamed-file.txt") is not None


class TestAbsorbMerge:
    def test_clean_merge(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.setup_baseline("public-file.txt", "line1\nline2\nline3\nline4\nline5\n")
        topo.commit_internal({"public-file.txt": "INTERNAL\nline2\nline3\nline4\nline5\n"})
        topo.commit_to_public({"public-file.txt": "line1\nline2\nline3\nline4\nPUBLIC\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "merge (clean)" in caplog.text

        merged = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "public-file.txt")
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

        conflicted = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "public-file.txt")
        assert conflicted is not None
        assert "<<<<<<<" in conflicted
        assert "=======" in conflicted
        assert ">>>>>>>" in conflicted
        assert "internal version" in conflicted
        assert "public version" in conflicted

        # Commit message should mention the conflicted file
        commit_msg = topo.work_dir.run("log", "-1", "--format=%B", topo.cfg.internal_absorb_branch).strip()
        assert "CONFLICTS" in commit_msg
        assert "public-file.txt" in commit_msg

    def test_sequential_merges_use_correct_base(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.setup_baseline("public-file.txt", "line1\nline2\nline3\n")

        # Round 1: public changes line 3
        topo.commit_to_public({"public-file.txt": "line1\nline2\nPUBLIC-V1\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Internal changes line 1 (file now has both changes on main)
        topo.commit_internal({"public-file.txt": "INTERNAL-V2\nline2\nPUBLIC-V1\n"}, push=True)

        # Round 2: public changes line 3 again
        topo.commit_to_public({"public-file.txt": "line1\nline2\nPUBLIC-V2\n"})
        topo.pubgate.absorb()

        # The base for this merge is the round-1 public version (line1/line2/PUBLIC-V1).
        # Internal changed line 1 -> INTERNAL-V2, public changed line 3 -> PUBLIC-V2.
        # If base were wrong, internal's line-1 change would conflict.
        merged = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "public-file.txt")
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

    def test_dry_run_bootstrap(self, topo: Topology, caplog):
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb(dry_run=True)
        assert "[dry-run]" in caplog.text
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_absorb_branch)

    def test_dirty_worktree_aborts(self, topo: Topology):
        (topo.work_dir.path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        topo.work_dir.run("add", "dirty.txt")
        with pytest.raises(PubGateError, match="not clean"):
            topo.pubgate.absorb()

    def test_added_text_file_unreadable_kept_local(self, topo: Topology, caplog):
        from unittest.mock import patch

        from pubgate.git import GitRepo

        topo.bootstrap_absorb()
        topo.commit_internal({"shared.txt": "local content\n"})
        topo.commit_to_public({"shared.txt": "public content\n"})

        original_read = GitRepo.read_file_at_ref

        def failing_read(self_inner, ref, path):
            if path == "shared.txt" and "public-remote" in ref:
                return None
            return original_read(self_inner, ref, path)

        with (
            patch.object(GitRepo, "read_file_at_ref", failing_read),
            caplog.at_level(logging.WARNING, logger="pubgate"),
        ):
            topo.pubgate.absorb()

        assert "kept local" in caplog.text


class TestAbsorbBinary:
    def test_binary_file_added(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"logo.png": SAMPLE_PNG})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_absorb_branch, "logo.png")
        assert absorbed == SAMPLE_PNG

    def test_non_utf8_file_absorbed_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"legacy.txt": SAMPLE_LATIN1})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_absorb_branch, "legacy.txt")
        assert absorbed == SAMPLE_LATIN1

    def test_binary_file_modified(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        binary_v1 = b"\x00\x01\x02\x03"
        binary_v2 = b"\x00\x04\x05\x06"
        topo.commit_to_public({"data.bin": binary_v1})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")
        topo.commit_to_public({"data.bin": binary_v2})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "binary" in caplog.text.lower()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_absorb_branch, "data.bin")
        assert absorbed == binary_v2

    def test_binary_added_on_public_kept_local(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_internal({"shared.bin": b"\x00\x01\x02\x03"})
        topo.commit_to_public({"shared.bin": b"\x04\x05\x06\x07"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "kept local, review manually" in caplog.text
        assert "shared.bin" in caplog.text


class TestAbsorbStateValidation:
    def test_garbage_state_file_aborts(self, topo: Topology):
        # Write garbage to the inbound state file on main
        topo.commit_internal({topo.cfg.absorb_state_file: "not-a-valid-sha\n"}, push=True)

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

    def test_binary_modify_with_unreadable_content_raises(self, topo: Topology):
        from unittest.mock import patch

        from pubgate.errors import PubGateError
        from pubgate.git import GitRepo

        topo.bootstrap_absorb()
        topo.commit_to_public({"logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Now modify the binary on public
        topo.commit_to_public({"logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x02"})

        original_read = GitRepo.read_file_at_ref_bytes

        def failing_read(self_inner, ref, path):
            if path == "logo.png" and "public-remote" in ref:
                return None
            return original_read(self_inner, ref, path)

        with (
            patch.object(GitRepo, "read_file_at_ref_bytes", failing_read),
            pytest.raises(PubGateError, match="binary content is unreadable"),
        ):
            topo.pubgate.absorb()


class TestAbsorbMixedChanges:
    def test_mixed_binary_and_text_in_single_absorb(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"readme.txt": "hello world\n", "icon.png": SAMPLE_PNG}, "mixed text and binary")
        topo.pubgate.absorb()

        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "readme.txt")
        assert content is not None
        assert "hello world" in content

        absorbed_bin = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_absorb_branch, "icon.png")
        assert absorbed_bin == SAMPLE_PNG

    def test_text_file_becomes_binary(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # First, push a text file
        topo.commit_to_public({"data.dat": "text content\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Now replace with binary content
        binary_data = b"\x00\x01\x02\x03\x04\x05"
        topo.commit_to_public({"data.dat": binary_data})
        topo.pubgate.absorb()

        absorbed = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_absorb_branch, "data.dat")
        assert absorbed == binary_data

    def test_delete_and_add_simultaneous(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"old-file.txt": "will be deleted\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Delete old file, add new file in one commit
        topo.commit_to_public({"new-file.txt": "fresh content\n"}, "delete old, add new", delete=["old-file.txt"])
        topo.pubgate.absorb()

        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_absorb_branch)
        assert "new-file.txt" in files
        # Deleted files are kept locally (by design)
        assert "old-file.txt" in files


class TestAbsorbModifyAfterPublish:
    def test_no_external_changes(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "header\nline2\nline3\nfooter\n")
        internal_content = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nnew_line2\nline3\nfooter\n"
        topo.commit_internal({"shared.txt": internal_content})

        topo.do_full_publish_cycle()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "header" in absorbed
        assert "footer" in absorbed

    def test_with_external_changes(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "header\nline2\nline3\nfooter\n")
        internal_content = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nnew_line2\nline3\nfooter\n"
        topo.commit_internal({"shared.txt": internal_content})

        topo.do_full_publish_cycle()

        # External contributor adds a line (relative to published content)
        topo.commit_to_public({"shared.txt": "header\nnew_line2\nline3\nfooter\nexternal_fix\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "external_fix" in absorbed

    def test_real_conflict(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "header\nline2\nline3\nfooter\n")
        internal_content = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\nline3\nnew_footer\n"
        topo.commit_internal({"shared.txt": internal_content})

        topo.do_full_publish_cycle()

        # Internal changes footer again (unpublished)
        updated_internal = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\nline3\nours_footer\n"
        topo.commit_internal({"shared.txt": updated_internal})

        # External also changes footer (relative to published: new_footer)
        topo.commit_to_public({"shared.txt": "header\nline2\nline3\nexternal_footer\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "CONFLICTS" in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "ours_footer" in absorbed
        assert "external_footer" in absorbed

    def test_no_internal_blocks(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "line1\nline2\nline3\n")
        topo.commit_internal({"shared.txt": "line1\nmodified_line2\nline3\n"})

        topo.do_full_publish_cycle()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "modified_line2" in absorbed

    def test_second_publish_cycle(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "header\nline2\nline3\nline4\nfooter\n")

        # First cycle
        internal_v1 = "header\n# BEGIN-INTERNAL\nsecret_v1\n# END-INTERNAL\nnew_line2\nline3\nline4\nfooter\n"
        topo.commit_internal({"shared.txt": internal_v1})
        topo.do_full_publish_cycle()
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Second cycle: update secret + change visible lines
        internal_v2 = "header\n# BEGIN-INTERNAL\nsecret_v2\n# END-INTERNAL\nfinal_line2\nline3\nline4\nupdated_footer\n"
        topo.commit_internal({"shared.txt": internal_v2})
        topo.do_full_publish_cycle()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "secret_v2" in absorbed
        assert "final_line2" in absorbed
        assert "updated_footer" in absorbed
        assert "BEGIN-INTERNAL" in absorbed

    def test_with_unpublished_internal_changes(self, topo: Topology, caplog):
        topo.setup_baseline("shared.txt", "header\nline2\nline3\nfooter\n")
        internal_content = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nnew_line2\nline3\nfooter\n"
        topo.commit_internal({"shared.txt": internal_content})

        topo.do_full_publish_cycle()

        # More internal edits (unpublished) before absorb
        updated_content = (
            "header\n# BEGIN-INTERNAL\nsecret\nnew_secret_line\n# END-INTERNAL\n"
            "new_line2\nline3\nfooter\nnew_internal_line\n"
        )
        topo.commit_internal({"shared.txt": updated_content})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "shared.txt")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "new_secret_line" in absorbed
        assert "new_internal_line" in absorbed


class TestAbsorbMetadataOnly:
    def test_metadata_only_commits_update_tracking(self, topo: Topology, caplog):
        topo.bootstrap_absorb()

        # Do a stage+publish cycle so that state files change on public
        topo.commit_internal({"app.txt": "content\n"})
        topo.do_full_publish_cycle()

        # Absorb: public changed (state file + app.txt), should succeed
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        # The absorb branch should have been created successfully
        state = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, topo.cfg.absorb_state_file)
        assert state is not None


class TestAbsorbDryRunWithChanges:
    def test_dry_run_lists_changed_files(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_to_public({"dry-new.txt": "content\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb(dry_run=True)

        assert "[dry-run]" in caplog.text
        assert "dry-new.txt" in caplog.text
        # PR branch should not exist after dry-run
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_absorb_branch)


class TestMergeFileMissingLocally:
    def test_modified_on_public_but_missing_locally(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        # Baseline: file exists on public
        topo.commit_to_public({"only-public.txt": "original\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Remove the file locally
        topo.work_dir.delete_files(["only-public.txt"], "remove locally")
        topo.work_dir.push("origin", "main")

        # Modify on public
        topo.commit_to_public({"only-public.txt": "modified\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "missing locally" in caplog.text
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "only-public.txt")
        assert content is not None
        assert "modified" in content


class TestAbsorbAddMergeConflict:
    def test_is_add_with_published_base_conflict(self, topo: Topology, caplog):
        # Create a file internally with BEGIN-INTERNAL blocks
        internal_content = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nfooter\n"
        topo.commit_internal({"new-shared.txt": internal_content})

        # Stage and publish: file appears on public for the first time
        topo.bootstrap_absorb()
        topo.do_full_publish_cycle()

        # Internal changes footer
        updated_internal = "header\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nours_footer\n"
        topo.commit_internal({"new-shared.txt": updated_internal})

        # External also changes footer (conflict)
        topo.commit_to_public({"new-shared.txt": "header\nexternal_footer\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "CONFLICTS" in caplog.text
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_absorb_branch, "new-shared.txt")
        assert content is not None
        assert "ours_footer" in content
        assert "external_footer" in content


class TestAbsorbStageStateUnreadable:
    def test_warning_when_stage_state_unreadable(self, topo: Topology, caplog):
        topo.bootstrap_absorb()

        # Put an invalid SHA in the stage state file on public
        topo.commit_to_public({topo.cfg.stage_state_file: "not-a-valid-sha\n", "new.txt": "content\n"})

        with caplog.at_level(logging.WARNING, logger="pubgate"):
            topo.pubgate.absorb()

        assert "Could not read stage state" in caplog.text
