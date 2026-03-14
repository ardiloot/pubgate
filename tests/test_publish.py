import logging

import pytest
from conftest import SAMPLE_PNG, Topology

from pubgate.errors import PubGateError


class TestPublishBasic:
    def test_publishes_correct_snapshot(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"

        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "pubgate.toml" not in files
        assert topo.cfg.inbound_state_file in files

        content = topo.work_dir.read_file_at_ref(pr_ref, "file1.txt")
        assert content is not None
        assert "internal content" in content

        state = topo.work_dir.read_file_at_ref(pr_ref, topo.cfg.outbound_state_file)
        assert state is not None
        assert state.strip() == topo.work_dir.git.rev_parse("main")


class TestPublishGuards:
    def test_guard_no_outbound_state(self, topo: Topology):
        # Bootstrap absorb so public branch doesn't exist yet
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")
        with pytest.raises(PubGateError, match="stage"):
            topo.pubgate.publish()

    def test_guard_internal_pr_not_merged(self, topo: Topology):
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")
        topo.pubgate.stage()
        # Don't merge the outbound PR - public branch has no outbound state
        with pytest.raises(PubGateError, match="stage"):
            topo.pubgate.publish()

    def test_already_published(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.pubgate.publish()

        # Simulate the public PR being merged
        topo.work_dir.run("fetch", "public-remote")
        topo.merge_public_pr(topo.cfg.public_pr_branch, "main")

        # Now publish again should be a no-op
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.publish()
        assert "Already published" in caplog.text


class TestPublishDryRun:
    def test_dry_run_previews_without_pushing(self, topo: Topology, caplog):
        topo.stage_and_merge()
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.publish(dry_run=True)
        assert "[dry-run]" in caplog.text
        assert "file1.txt" in caplog.text

        topo.work_dir.run("fetch", "public-remote")
        result = topo.work_dir.run("branch", "-r").strip()
        assert "public-remote/sync-to-public" not in result


class TestPublishFullCycle:
    def test_absorb_after_publish_catches_up(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        # Merge the public PR
        topo.work_dir.run("fetch", "public-remote")
        topo.merge_public_pr(topo.cfg.public_pr_branch, "main")

        # Absorb catches up (updates tracking to new public-remote/main)
        topo.work_dir.run("checkout", "main")
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Stage should still work
        topo.pubgate.stage()

    def test_absorb_after_publish_preserves_internal_blocks(self, topo: Topology):
        """BEGIN-INTERNAL blocks survive a full publish → absorb round-trip."""
        internal_content = "public line\n# BEGIN-INTERNAL\nsecret()\n# END-INTERNAL\npublic end\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()

        # Verify staging stripped the internal block
        staged = topo.work_dir.read_file_at_ref(f"origin/{topo.cfg.internal_preview_branch}", "app.py")
        assert staged is not None
        assert "BEGIN-INTERNAL" not in staged
        assert "secret()" not in staged

        topo.pubgate.publish()

        # Merge the public PR
        topo.work_dir.run("fetch", "public-remote")
        topo.merge_public_pr(topo.cfg.public_pr_branch, "main")

        # Absorb after publish must preserve internal blocks
        topo.work_dir.run("checkout", "main")
        topo.pubgate.absorb()

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.inbound_pr_branch, "app.py")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret()" in absorbed
        assert "public line" in absorbed
        assert "public end" in absorbed


class TestPublishBinary:
    def test_binary_file_published_intact(self, topo: Topology):
        # Add a binary file to internal main before the stage cycle
        topo.commit_internal({"asset.png": SAMPLE_PNG})

        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        published = topo.work_dir.git.read_file_at_ref_bytes(f"public-remote/{topo.cfg.public_pr_branch}", "asset.png")
        assert published == SAMPLE_PNG


class TestPublishRepublish:
    def test_republish_force_pushes_over_existing_branch(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()
        # Don't merge the public PR - branch still exists on public

        # Make a new internal change, stage, merge, and publish again
        topo.commit_internal({"v2.txt": "version 2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)
        topo.pubgate.publish(force=True)

        # Verify new content is on public sync branch
        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        content = topo.work_dir.read_file_at_ref(pr_ref, "v2.txt")
        assert content is not None
        assert "version 2" in content


class TestPublishWithExternals:
    def test_publish_proceeds_with_unabsorbed_externals(self, topo: Topology):
        """Publish succeeds even when externals arrived after staging."""
        topo.stage_and_merge()

        # External contribution arrives after staging
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Absorb the external change into internal main
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Publish should succeed: staged snapshot is based on absorbed commit
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files

    def test_full_cycle_after_external_contribution(self, topo: Topology):
        """After external contribution, re-stage + publish delivers complete snapshot."""
        topo.stage_and_merge()

        # External contribution arrives
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Absorb
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Re-stage (includes external content now absorbed into main)
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)

        # Publish succeeds
        topo.pubgate.publish()

        # Verify public PR contains both internal and external content
        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "external.txt" in files


class TestPublishStageAbsorbPublish:
    def test_stage_then_absorb_then_publish(self, topo: Topology):
        """stage → absorb → publish: staging before absorbing externals, then publishing."""
        topo.bootstrap_absorb()

        # External contribution arrives before staging
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Stage without absorbing first (allowed now)
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)

        # Now absorb the external contribution
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.inbound_pr_branch, "main")

        # Publish the already-staged content
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        # Staged content doesn't include the external file (wasn't absorbed at stage time)
        assert "file1.txt" in files
        assert "external.txt" not in files


class TestPublishFromNonMain:
    def test_publish_succeeds_from_different_branch(self, topo: Topology):
        topo.stage_and_merge()

        # Switch to a different branch before publishing
        topo.work_dir.run("checkout", "-b", "feature-branch")
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files


class TestPublishBranchGuard:
    def test_errors_when_pr_branch_exists(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        # Make a new change cycle and try to publish again - should refuse
        topo.commit_internal({"v2.txt": "v2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)
        with pytest.raises(PubGateError, match="--force"):
            topo.pubgate.publish()

    def test_force_overwrites_existing_branch(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.commit_internal({"v2.txt": "v2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.outbound_pr_branch, topo.cfg.internal_preview_branch)
        topo.pubgate.publish(force=True)

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.public_pr_branch}"
        assert "v2.txt" in topo.work_dir.list_files_at_ref(pr_ref)


class TestPublishAncestryValidation:
    def test_publish_rejects_unreachable_absorbed_sha(self, topo: Topology):
        """Publish must refuse if absorbed_sha is not an ancestor of public/main."""
        topo.stage_and_merge()

        # Force-push public/main to a completely new commit, making the absorbed SHA unreachable
        topo.external_contributor.run("checkout", "main")
        (topo.external_contributor.path / "reset.txt").write_text("reset\n", encoding="utf-8")
        topo.external_contributor.run("add", "-A")
        topo.external_contributor.run("commit", "--amend", "-m", "rewritten history")
        topo.external_contributor.run("push", "--force", "origin", "main")

        with pytest.raises(PubGateError, match="not an ancestor"):
            topo.pubgate.publish()
