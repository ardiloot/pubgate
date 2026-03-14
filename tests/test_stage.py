import logging

import pytest
from conftest import SAMPLE_LATIN1, SAMPLE_PNG, Topology

from pubgate.config import Config
from pubgate.errors import PubGateError


class TestStageConflictMarkers:
    def test_stage_refuses_unresolved_conflict_markers(self, topo: Topology):
        """Stage must reject files that contain unresolved merge-conflict markers."""
        topo.bootstrap_absorb()
        # Setup: create a file on both sides, then absorb with a conflict
        topo.setup_baseline("shared.txt", "original line\n")
        topo.commit_internal({"shared.txt": "internal version\n"}, push=True)
        topo.commit_to_public({"shared.txt": "public version\n"})
        topo.pubgate.absorb()
        # Merge the absorb PR WITHOUT resolving the conflict markers
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Stage should refuse because shared.txt has conflict markers
        with pytest.raises(PubGateError, match="merge-conflict marker"):
            topo.pubgate.stage()


class TestStageBasic:
    def test_stage_creates_correct_snapshot(self, topo: Topology):
        topo.bootstrap_absorb()
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_preview_branch)
        topo.pubgate.stage()

        assert topo.work_dir.git.branch_exists(topo.cfg.outbound_pr_branch)
        assert topo.work_dir.git.branch_exists(topo.cfg.internal_preview_branch)

        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "file1.txt" in files
        assert topo.cfg.inbound_state_file in files
        assert "pubgate.toml" not in files

        content = topo.work_dir.read_file_at_ref(topo.cfg.outbound_pr_branch, "file1.txt")
        assert content is not None
        assert "internal content" in content

        state = topo.work_dir.read_file_at_ref(topo.cfg.outbound_pr_branch, topo.cfg.outbound_state_file)
        assert state is not None
        assert state.strip() == topo.work_dir.git.rev_parse("main")


class TestStageIdempotent:
    def test_staging_twice_produces_same_files(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        ob = topo.cfg.outbound_pr_branch
        state_file = topo.cfg.outbound_state_file

        def snap():
            return {
                f: topo.work_dir.read_file_at_ref(ob, f) for f in topo.work_dir.list_files_at_ref(ob) if f != state_file
            }

        first = snap()
        topo.pubgate.stage()
        assert snap() == first


class TestStageGuards:
    def test_stage_errors_without_bootstrap(self, topo: Topology):
        # No absorb has been run, so .pubgate-state-inbound doesn't exist
        with pytest.raises(PubGateError, match="no inbound state found"):
            topo.pubgate.stage()

    def test_stage_proceeds_with_unabsorbed_externals(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"newer.txt": "newer\n"})
        topo.pubgate.stage()  # Should succeed even with unabsorbed externals
        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "file1.txt" in files
        assert "newer.txt" not in files  # Not absorbed, so not in snapshot

    def test_restage_without_publishing_succeeds(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)
        # New change without publishing previous outbound
        topo.commit_internal({"another.txt": "another\n"})
        topo.pubgate.stage()  # Should succeed without requiring publish first
        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "another.txt" in files

    def test_restage_then_publish_delivers_updated_content(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)

        # New internal change, then restage
        topo.commit_internal({"v2.txt": "version 2\n"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)

        # Publish delivers the restaged content
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "v2.txt" in files


class TestStageDryRun:
    def test_dry_run_previews_without_side_effects(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_preview_branch)
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage(dry_run=True)
        assert "[dry-run]" in caplog.text
        assert "file1.txt" in caplog.text
        assert not topo.work_dir.git.branch_exists(topo.cfg.outbound_pr_branch)
        assert not topo.work_dir.git.branch_exists(topo.cfg.internal_preview_branch)


class TestStageBinary:
    def test_binary_file_staged_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"icon.png": SAMPLE_PNG})
        topo.pubgate.stage()

        # Read binary content back from the staged branch
        staged = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.outbound_pr_branch, "icon.png")
        assert staged == SAMPLE_PNG

    def test_non_utf8_file_staged_intact(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"latin1.txt": SAMPLE_LATIN1})
        topo.pubgate.stage()

        staged = topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.outbound_pr_branch, "latin1.txt")
        assert staged == SAMPLE_LATIN1


class TestStageAllFiltered:
    def test_all_files_ignored_stages_only_state(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.cfg = Config(ignore=["*.txt"])
        topo.pubgate.stage()
        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        # No user files, only state files remain
        assert "file1.txt" not in files
        assert topo.cfg.inbound_state_file in files
        assert topo.cfg.outbound_state_file in files


class TestStageEmptyAfterScrub:
    def test_file_empty_after_scrub_included(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"allsecret.py": "# BEGIN-INTERNAL\nall secret content\n# END-INTERNAL\n"})
        topo.pubgate.stage()
        # _build_outbound_snapshot includes scrubbed (empty) files
        content = topo.work_dir.read_file_at_ref(topo.cfg.outbound_pr_branch, "allsecret.py")
        assert content is not None
        assert content.strip() == ""


class TestStageRetroactiveIgnore:
    def test_new_ignore_pattern_excludes_previously_staged_file(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal({"secret.dat": "data\n"})

        # First stage includes the file
        topo.pubgate.stage()
        files_v1 = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "secret.dat" in files_v1

        # Add ignore pattern and re-stage
        topo.cfg = Config(ignore=["*.dat"])
        topo.pubgate.stage(force=True)
        files_v2 = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "secret.dat" not in files_v2


class TestStageInternalOnlyChanges:
    def test_internal_only_change_is_noop(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_internal({"mixed.py": "public code\n# BEGIN-INTERNAL\ninternal v1\n# END-INTERNAL\n"})
        topo.pubgate.stage()

        # Change only the internal block
        topo.commit_internal({"mixed.py": "public code\n# BEGIN-INTERNAL\ninternal v2 changed\n# END-INTERNAL\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert "No changes" in caplog.text or "up to date" in caplog.text


class TestStageStaleOutbound:
    def test_stage_allowed_when_outbound_stale(self, topo: Topology):
        """stage() must succeed when pending outbound is stale (external changes absorbed)."""
        topo.stage_and_merge()

        # External contribution arrives and is absorbed
        topo.commit_to_public({"external.txt": "external fix\n"})
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # stage() should succeed despite pending outbound (no 'publish first' error)
        topo.pubgate.stage()

        # Verify re-staged content includes both internal and external files
        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "file1.txt" in files
        assert "external.txt" in files


class TestStageBranchGuard:
    def test_errors_when_pr_branch_exists(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        assert topo.work_dir.git.branch_exists(topo.cfg.outbound_pr_branch)

        # Add a new file and try to stage again - should refuse without --force
        topo.commit_internal({"new.txt": "new\n"})
        with pytest.raises(PubGateError, match="--force"):
            topo.pubgate.stage()

    def test_force_overwrites_existing_branch(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.pubgate.stage()

        topo.commit_internal({"new.txt": "new\n"})
        topo.pubgate.stage(force=True)
        files = topo.work_dir.list_files_at_ref(topo.cfg.outbound_pr_branch)
        assert "new.txt" in files
