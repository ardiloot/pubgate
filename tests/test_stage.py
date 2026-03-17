import logging

import pytest
from conftest import SAMPLE_LATIN1, SAMPLE_PNG, Topology

from pubgate.config import Config
from pubgate.errors import PubGateError


class TestStageConflictMarkers:
    def test_stage_refuses_unresolved_conflict_markers(self, topo: Topology):
        topo.bootstrap_absorb()
        # Setup: create a file on both sides, then absorb with a conflict
        topo.setup_baseline("shared.txt", "original line\n")
        topo.commit_internal({"shared.txt": "internal version\n"}, push=True)
        topo.commit_to_public({"shared.txt": "public version\n"})
        topo.pubgate.absorb()
        # Merge the absorb PR WITHOUT resolving the conflict markers
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # Stage should refuse because shared.txt has conflict markers
        with pytest.raises(PubGateError, match="merge-conflict marker"):
            topo.pubgate.stage()


class TestStageBasic:
    def test_stage_creates_correct_snapshot(self, topo: Topology):
        topo.bootstrap_absorb()
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_approved_branch)
        topo.pubgate.stage()

        assert topo.work_dir.git.branch_exists(topo.cfg.internal_stage_branch)
        assert topo.work_dir.git.branch_exists(topo.cfg.internal_approved_branch)

        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "file1.txt" in files
        assert topo.cfg.absorb_state_file in files
        assert "pubgate.toml" not in files

        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_stage_branch, "file1.txt")
        assert content is not None
        assert "internal content" in content

        state = topo.work_dir.read_file_at_ref(topo.cfg.internal_stage_branch, topo.cfg.stage_state_file)
        assert state is not None
        assert state.strip() == topo.work_dir.git.rev_parse("main")


class TestStageIdempotent:
    def test_staging_twice_errors_without_force(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        with pytest.raises(PubGateError, match="already exists"):
            topo.pubgate.stage()

    def test_staging_twice_with_force_produces_same_files(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        ob = topo.cfg.internal_stage_branch
        state_file = topo.cfg.stage_state_file

        def snap():
            return {
                f: topo.work_dir.read_file_at_ref(ob, f) for f in topo.work_dir.list_files_at_ref(ob) if f != state_file
            }

        first = snap()
        topo.pubgate.stage(force=True)
        assert snap() == first


class TestStageGuards:
    def test_stage_errors_without_bootstrap(self, topo: Topology):
        # No absorb has been run, so .pubgate-absorbed doesn't exist
        with pytest.raises(PubGateError, match="no absorb state found"):
            topo.pubgate.stage()

    def test_stage_proceeds_with_unabsorbed_externals(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"newer.txt": "newer\n"})
        topo.pubgate.stage()  # Should succeed even with unabsorbed externals
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "file1.txt" in files
        assert "newer.txt" not in files  # Not absorbed, so not in snapshot

    def test_restage_without_publishing_succeeds(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        # New change without publishing previous outbound
        topo.commit_internal({"another.txt": "another\n"})
        topo.pubgate.stage()  # Should succeed without requiring publish first
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "another.txt" in files

    def test_restage_then_publish_delivers_updated_content(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)

        # New internal change, then restage
        topo.commit_internal({"v2.txt": "version 2\n"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)

        # Publish delivers the restaged content
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_publish_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "v2.txt" in files


class TestStageDryRun:
    def test_dry_run_previews_without_side_effects(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_approved_branch)
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage(dry_run=True)
        assert "[dry-run] Would commit on" in caplog.text
        assert "[dry-run] Would push" in caplog.text
        assert "Next steps" in caplog.text
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_stage_branch)
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_approved_branch)


class TestStageBinary:
    def test_binary_file_staged_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"icon.png": SAMPLE_PNG})
        topo.pubgate.stage()

        # Read binary content back from the staged branch
        staged = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "icon.png")
        assert staged == SAMPLE_PNG

    def test_non_utf8_file_staged_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"latin1.txt": SAMPLE_LATIN1})
        topo.pubgate.stage()

        staged = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "latin1.txt")
        assert staged == SAMPLE_LATIN1


class TestStageAllFiltered:
    def test_all_files_ignored_stages_only_state(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.cfg = Config(ignore=["*.txt"])
        topo.pubgate.stage()
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        # No user files, only state files remain
        assert "file1.txt" not in files
        assert topo.cfg.absorb_state_file in files
        assert topo.cfg.stage_state_file in files


class TestStageEmptyAfterScrub:
    def test_file_empty_after_scrub_included(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"allsecret.py": "# BEGIN-INTERNAL\nall secret content\n# END-INTERNAL\n"})
        topo.pubgate.stage()
        # _build_stage_snapshot includes scrubbed (empty) files
        content = topo.work_dir.read_file_at_ref(topo.cfg.internal_stage_branch, "allsecret.py")
        assert content is not None
        assert content.strip() == ""


class TestStageRetroactiveIgnore:
    def test_new_ignore_pattern_excludes_previously_staged_file(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"secret.dat": "data\n"})

        # First stage includes the file
        topo.pubgate.stage()
        files_v1 = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "secret.dat" in files_v1

        # Add ignore pattern and re-stage
        topo.cfg = Config(ignore=["*.dat"])
        topo.pubgate.stage(force=True)
        files_v2 = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "secret.dat" not in files_v2


class TestStageInternalOnlyChanges:
    def test_internal_only_change_errors_without_force(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"mixed.py": "public code\n# BEGIN-INTERNAL\ninternal v1\n# END-INTERNAL\n"})
        topo.pubgate.stage()

        # Change only the internal block
        topo.commit_internal({"mixed.py": "public code\n# BEGIN-INTERNAL\ninternal v2 changed\n# END-INTERNAL\n"})
        with pytest.raises(PubGateError, match="already exists"):
            topo.pubgate.stage()


class TestStageStaleOutbound:
    def test_stage_allowed_when_outbound_stale(self, topo: Topology):
        topo.stage_and_merge()

        # External contribution arrives and is absorbed
        topo.commit_to_public({"external.txt": "external fix\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")

        # stage() should succeed despite pending stage (no 'publish first' error)
        topo.pubgate.stage()

        # Verify re-staged content includes both internal and external files
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "file1.txt" in files
        assert "external.txt" in files


class TestStageBranchGuard:
    def test_errors_when_pr_branch_exists(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        assert topo.work_dir.git.branch_exists(topo.cfg.internal_stage_branch)

        # Add a new file and try to stage again - should refuse without --force
        topo.commit_internal({"new.txt": "new\n"})
        with pytest.raises(PubGateError, match="--force"):
            topo.pubgate.stage()

    def test_force_overwrites_existing_branch(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()

        topo.commit_internal({"new.txt": "new\n"})
        topo.pubgate.stage(force=True)
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "new.txt" in files


class TestStageSkipsStateOnly:
    def test_skips_when_only_absorb_state_changed(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Stage should see no content changes and skip
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert "No changes to stage" in caplog.text

    def test_proceeds_when_state_and_content_changed(self, topo: Topology):
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Make a real content change
        topo.commit_internal({"new-feature.txt": "feature code\n"})

        # Stage should proceed (content changed, not just state)
        topo.pubgate.stage()
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "new-feature.txt" in files

    def test_skips_when_crlf_only_diff(self, topo: Topology, caplog):
        # Write a file with CRLF endings on the approved branch
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Force CRLF content into the approved branch via a direct commit
        from conftest import _git

        merge_dir = topo.tmp_dir / "crlf-setup"
        _git(topo.tmp_dir, "clone", str(topo.internal_server.bare_path), str(merge_dir))
        _git(merge_dir, "checkout", topo.cfg.internal_approved_branch)
        (merge_dir / "crlf-file.txt").write_bytes(b"line1\r\nline2\r\n")
        _git(merge_dir, "-c", "core.autocrlf=false", "add", "crlf-file.txt")
        _git(merge_dir, "commit", "-m", "add crlf file")
        _git(merge_dir, "push", "origin", topo.cfg.internal_approved_branch)
        topo.work_dir.fetch("origin")

        # Also add the same file (with CRLF) to main
        (topo.work_dir.path / "crlf-file.txt").write_bytes(b"line1\r\nline2\r\n")
        _git(topo.work_dir.path, "-c", "core.autocrlf=false", "add", "crlf-file.txt")
        _git(topo.work_dir.path, "commit", "-m", "add crlf file on main")
        topo.work_dir.push("origin", "main")

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert "No changes to stage" in caplog.text


class TestStageLogOnelineException:
    def test_stage_succeeds_when_log_oneline_fails(self, topo: Topology, caplog):
        from unittest.mock import patch

        from pubgate.errors import PubGateError
        from pubgate.git import GitRepo

        topo.stage_and_merge()

        # Publish + absorb to complete the cycle, then make a new change
        topo.publish_and_merge()
        topo.absorb_and_merge()
        topo.commit_internal({"v2.txt": "version 2\n"})

        call_count = 0
        original_log = GitRepo.log_oneline

        def failing_first_log(self_inner, base, head):
            nonlocal call_count
            call_count += 1
            # Fail only on the first call (the pre-commit logging in stage()),
            # let subsequent calls (e.g., commit message generation) succeed.
            if call_count == 1:
                raise PubGateError("simulated log failure")
            return original_log(self_inner, base, head)

        with patch.object(GitRepo, "log_oneline", failing_first_log):
            with caplog.at_level(logging.INFO, logger="pubgate"):
                topo.pubgate.stage(force=True)

        # Stage should still succeed
        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "v2.txt" in files
        assert call_count >= 1


class TestSnapshotUnreadableFile:
    def test_unreadable_file_skipped(self, topo: Topology):
        from unittest.mock import patch

        from pubgate.git import GitRepo

        topo.bootstrap_absorb()
        topo.commit_internal({"normal.txt": "ok\n", "broken.txt": "will-be-unreadable\n"})

        original_read = GitRepo.read_file_auto

        def selective_read(self_inner, ref, path):
            if path == "broken.txt":
                return None
            return original_read(self_inner, ref, path)

        with patch.object(GitRepo, "read_file_auto", selective_read):
            topo.pubgate.stage()

        files = topo.work_dir.list_files_at_ref(topo.cfg.internal_stage_branch)
        assert "normal.txt" in files
        assert "broken.txt" not in files


class TestEnsurePublicBranchCleanup:
    def test_orphan_branch_cleanup_on_failure(self, topo: Topology, caplog):
        from unittest.mock import patch

        from pubgate.git import GitRepo
        from pubgate.stage_snapshot import ensure_public_branch

        topo.bootstrap_absorb()

        # Ensure the public-approved branch doesn't exist on origin (may not have been created yet)
        # Delete the remote tracking ref if it exists
        preview_branch = topo.cfg.internal_approved_branch
        try:
            topo.work_dir.run("rev-parse", "--verify", f"refs/remotes/origin/{preview_branch}")
            topo.work_dir.run("push", "origin", "--delete", preview_branch)
        except Exception:
            pass
        # Also delete local branch if it exists
        if topo.work_dir.git.branch_exists(preview_branch):
            topo.work_dir.run("branch", "-D", preview_branch)
        topo.work_dir.fetch("origin")

        def failing_commit(self_inner, msg):
            raise RuntimeError("simulated commit failure")

        with (
            patch.object(GitRepo, "commit_allow_empty", failing_commit),
            caplog.at_level(logging.WARNING, logger="pubgate"),
            pytest.raises(RuntimeError, match="simulated commit failure"),
        ):
            ensure_public_branch(topo.pubgate.cfg, topo.pubgate.git)

        # Should be back on main, orphan branch should be cleaned up
        current = topo.work_dir.run("rev-parse", "--abbrev-ref", "HEAD").strip()
        assert current == "main"


class TestScrubResidualMarkerInStage:
    def test_unclosed_begin_marker_raises(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"oops.txt": "public\n# BEGIN-INTERNAL\nsecret stays\n"})

        from pubgate.errors import PubGateError

        with pytest.raises(PubGateError, match="unclosed BEGIN-INTERNAL"):
            topo.pubgate.stage()


class TestStageBinarySnapshotComparison:
    def test_binary_file_change_detected_in_snapshot(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"asset.bin": b"\x00\x01\x02"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.work_dir.run("checkout", "main")

        # Change the binary content
        topo.commit_internal({"asset.bin": b"\x03\x04\x05"})
        # Stage should detect the change and proceed
        topo.pubgate.stage(force=True)
        staged = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "asset.bin")
        assert staged == b"\x03\x04\x05"
