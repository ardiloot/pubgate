import logging

import pytest
from conftest import SAMPLE_PNG, Topology

from pubgate.errors import PubGateError


class TestPublishBasic:
    def test_publishes_correct_snapshot(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"

        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "pubgate.toml" not in files
        assert topo.cfg.absorb_state_file in files

        content = topo.work_dir.read_file_at_ref(pr_ref, "file1.txt")
        assert content is not None
        assert "internal content" in content

        state = topo.work_dir.read_file_at_ref(pr_ref, topo.cfg.stage_state_file)
        assert state is not None
        assert state.strip() == topo.work_dir.git.rev_parse("main")


class TestPublishGuards:
    def test_guard_no_stage_state(self, topo: Topology):
        # Bootstrap absorb so public branch doesn't exist yet
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.absorb_pr_branch, "main")
        with pytest.raises(PubGateError, match="stage"):
            topo.pubgate.publish()

    def test_guard_internal_pr_not_merged(self, topo: Topology):
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.absorb_pr_branch, "main")
        topo.pubgate.stage()
        # Don't merge the stage PR - public branch has no stage state
        with pytest.raises(PubGateError, match="stage"):
            topo.pubgate.publish()

    def test_already_published(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.publish_and_merge()

        # Now publish again should be a no-op
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.publish()
        assert "Already published" in caplog.text


class TestPublishDryRun:
    def test_dry_run_previews_without_pushing(self, topo: Topology, caplog):
        topo.stage_and_merge()
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.publish(dry_run=True)
        assert "[dry-run] Would commit on" in caplog.text
        assert "[dry-run] Would push" in caplog.text
        assert "Next steps" in caplog.text

        topo.work_dir.run("fetch", "public-remote")
        result = topo.work_dir.run("branch", "-r").strip()
        assert "public-remote/sync-to-public" not in result


class TestPublishFullCycle:
    def test_absorb_after_publish_catches_up(self, topo: Topology):
        topo.stage_and_merge()
        topo.publish_and_merge()

        # Absorb catches up (updates tracking to new public-remote/main)
        topo.absorb_and_merge()

        # Stage should still work
        topo.pubgate.stage()

    def test_absorb_after_publish_preserves_internal_blocks(self, topo: Topology, caplog):
        internal_content = "public line\n# BEGIN-INTERNAL\nsecret()\n# END-INTERNAL\npublic end\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()

        # Verify staging stripped the internal block
        staged = topo.work_dir.read_file_at_ref(f"origin/{topo.cfg.internal_preview_branch}", "app.py")
        assert staged is not None
        assert "BEGIN-INTERNAL" not in staged
        assert "secret()" not in staged

        topo.publish_and_merge()

        # Absorb after publish must preserve internal blocks
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        # Should be a clean merge, no warnings
        assert "merge (clean): app.py" in caplog.text
        assert "kept local version" not in caplog.text
        assert "CONFLICTS" not in caplog.text

        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret()" in absorbed
        assert "public line" in absorbed
        assert "public end" in absorbed

    def test_absorb_after_publish_preserves_unpublished_internal_changes(self, topo: Topology, caplog):
        internal_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()
        topo.publish_and_merge()

        # Make internal changes AFTER publish (like adding print("Tere"))
        new_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\nnew_internal_line\n"
        topo.commit_internal({"app.py": new_content})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean): app.py" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "new_internal_line" in absorbed

    def test_absorb_after_publish_integrates_external_contribution(self, topo: Topology, caplog):
        internal_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()
        topo.publish_and_merge()

        # External contributor changes the file on public
        topo.commit_to_public({"app.py": "line1\nline2\nexternal fix\n"})
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "external fix" in absorbed

    def test_absorb_after_publish_merges_both_changes(self, topo: Topology, caplog):
        internal_content = "line1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\nline3\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()
        topo.publish_and_merge()

        # Internal changes line1
        new_internal = "CHANGED_LINE1\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nline2\nline3\n"
        topo.commit_internal({"app.py": new_internal})

        # External contributor changes line3
        topo.commit_to_public({"app.py": "line1\nline2\nEXTERNAL_LINE3\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean)" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "CHANGED_LINE1" in absorbed
        assert "BEGIN-INTERNAL" in absorbed
        assert "secret" in absorbed
        assert "EXTERNAL_LINE3" in absorbed

    def test_absorb_after_publish_no_internal_blocks(self, topo: Topology, caplog):
        topo.commit_internal({"plain.txt": "line1\nline2\n"})

        topo.stage_and_merge()
        topo.publish_and_merge()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean): plain.txt" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "plain.txt")
        assert absorbed is not None
        assert absorbed.strip() == "line1\nline2"

    def test_absorb_after_publish_conflict(self, topo: Topology, caplog):
        internal_content = "line1\nline2\nline3\n"
        topo.commit_internal({"app.py": internal_content})

        topo.stage_and_merge()
        topo.publish_and_merge()

        # Internal edits line2
        topo.commit_internal({"app.py": "line1\nINTERNAL_CHANGE\nline3\n"})

        # External also edits line2
        topo.commit_to_public({"app.py": "line1\nEXTERNAL_CHANGE\nline3\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "CONFLICTS" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "<<<<<<" in absorbed
        assert "INTERNAL_CHANGE" in absorbed
        assert "EXTERNAL_CHANGE" in absorbed

    def test_absorb_second_publish_cycle(self, topo: Topology, caplog):
        internal_v1 = (
            "header\nline2\nline3\nline4\nline5\n"
            "# BEGIN-INTERNAL\nsecret_v1\n# END-INTERNAL\n"
            "line6\nline7\nline8\nfooter\n"
        )
        topo.commit_internal({"app.py": internal_v1})

        # First cycle: stage → publish → merge public PR → absorb → merge absorb PR
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Update internal: change footer (far from internal block)
        internal_v2 = (
            "header\nline2\nline3\nline4\nline5\n"
            "# BEGIN-INTERNAL\nsecret_v2\n# END-INTERNAL\n"
            "line6\nline7\nline8\nfooter_updated\n"
        )
        topo.commit_internal({"app.py": internal_v2})

        # Second cycle: stage → publish → merge public PR → absorb
        topo.do_full_publish_cycle()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "merge (clean): app.py" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "app.py")
        assert absorbed is not None
        assert "footer_updated" in absorbed
        assert "secret_v2" in absorbed
        assert "BEGIN-INTERNAL" in absorbed

    def test_absorb_file_added_locally_after_stage(self, topo: Topology, caplog):
        topo.commit_internal({"existing.txt": "existing\n"})

        topo.stage_and_merge()
        topo.publish_and_merge()

        # Add a NEW file locally that wasn't part of the stage
        topo.commit_internal({"new_local.txt": "local only content\n"})

        # External contributor adds the same filename on public
        topo.commit_to_public({"new_local.txt": "external content\n"})

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        # staged_sha exists but new_local.txt wasn't at that commit → fallback
        assert "kept local" in caplog.text
        absorbed = topo.work_dir.read_file_at_ref(topo.cfg.absorb_pr_branch, "new_local.txt")
        assert absorbed is not None
        assert "local only content" in absorbed


class TestPublishBinary:
    def test_binary_file_published_intact(self, topo: Topology):
        # Add a binary file to internal main before the stage cycle
        topo.commit_internal({"asset.png": SAMPLE_PNG})

        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        published = topo.work_dir.git.read_file_at_ref_bytes(f"public-remote/{topo.cfg.publish_pr_branch}", "asset.png")
        assert published == SAMPLE_PNG


class TestPublishRepublish:
    def test_republish_force_pushes_over_existing_branch(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()
        # Don't merge the public PR - branch still exists on public

        # Make a new internal change, stage, merge, and publish again
        topo.commit_internal({"v2.txt": "version 2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)
        topo.pubgate.publish(force=True)

        # Verify new content is on public sync branch
        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
        content = topo.work_dir.read_file_at_ref(pr_ref, "v2.txt")
        assert content is not None
        assert "version 2" in content


class TestPublishWithExternals:
    def test_publish_proceeds_with_unabsorbed_externals(self, topo: Topology):
        topo.stage_and_merge()

        # External contribution arrives after staging
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Absorb the external change into internal main
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.absorb_pr_branch, "main")

        # Publish should succeed: staged snapshot is based on absorbed commit
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files

    def test_full_cycle_after_external_contribution(self, topo: Topology):
        topo.stage_and_merge()

        # External contribution arrives
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Absorb
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.absorb_pr_branch, "main")

        # Re-stage (includes external content now absorbed into main)
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)

        # Publish succeeds
        topo.pubgate.publish()

        # Verify public PR contains both internal and external content
        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files
        assert "external.txt" in files


class TestPublishStageAbsorbPublish:
    def test_stage_then_absorb_then_publish(self, topo: Topology):
        topo.bootstrap_absorb()

        # External contribution arrives before staging
        topo.commit_to_public({"external.txt": "external fix\n"})

        # Stage without absorbing first (allowed now)
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)

        # Now absorb the external contribution
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.absorb_pr_branch, "main")

        # Publish the already-staged content
        topo.pubgate.publish()

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
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
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
        files = topo.work_dir.list_files_at_ref(pr_ref)
        assert "file1.txt" in files


class TestPublishBranchGuard:
    def test_errors_when_pr_branch_exists(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        # Make a new change cycle and try to publish again - should refuse
        topo.commit_internal({"v2.txt": "v2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)
        with pytest.raises(PubGateError, match="--force"):
            topo.pubgate.publish()

    def test_force_overwrites_existing_branch(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        topo.commit_internal({"v2.txt": "v2\n"}, push=True)
        topo.pubgate.stage(force=True)
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)
        topo.pubgate.publish(force=True)

        topo.work_dir.run("fetch", "public-remote")
        pr_ref = f"public-remote/{topo.cfg.publish_pr_branch}"
        assert "v2.txt" in topo.work_dir.list_files_at_ref(pr_ref)


class TestPublishAncestryValidation:
    def test_publish_rejects_unreachable_absorbed_sha(self, topo: Topology):
        topo.stage_and_merge()

        # Force-push public/main to a completely new commit, making the absorbed SHA unreachable
        topo.external_contributor.run("checkout", "main")
        (topo.external_contributor.path / "reset.txt").write_text("reset\n", encoding="utf-8")
        topo.external_contributor.run("add", "-A")
        topo.external_contributor.run("commit", "--amend", "-m", "rewritten history")
        topo.external_contributor.run("push", "--force", "origin", "main")

        with pytest.raises(PubGateError, match="not an ancestor"):
            topo.pubgate.publish()


class TestPublishBaseAdvancement:
    def test_publish_base_advances_when_no_external_changes(self, topo: Topology, caplog):
        topo.commit_internal({"app.txt": "v1\n"})
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Make a new internal change and re-publish
        topo.commit_internal({"app.txt": "v2\n"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)
        topo.work_dir.run("checkout", "main")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.publish()

        # Should succeed without errors
        topo.work_dir.run("fetch", "public-remote")
        published = topo.work_dir.read_file_at_ref(f"public-remote/{topo.cfg.publish_pr_branch}", "app.txt")
        assert published is not None
        assert "v2" in published


class TestPublishForcePushProtection:
    def test_force_push_to_protected_branch_rejected(self, topo: Topology):
        topo.stage_and_merge()
        topo.pubgate.publish()

        # Try to force-push to internal main — should fail
        with pytest.raises(PubGateError, match="refusing to force-push"):
            topo.pubgate._push_to_remote("main", "origin", "main", force=True)

    def test_force_push_to_pr_branch_allowed(self, topo: Topology):
        topo.stage_and_merge()
        # Push to a custom branch first, then force-push
        topo.work_dir.git.push(topo.cfg.stage_pr_branch, "origin", topo.cfg.stage_pr_branch)
        # Force push should not raise (PR branches are not protected)
        topo.pubgate._push_to_remote(topo.cfg.stage_pr_branch, "origin", topo.cfg.stage_pr_branch, force=True)


class TestPublishNoChanges:
    def test_publish_noop_when_already_published(self, topo: Topology, caplog):
        topo.commit_internal({"app.txt": "v1\n"})
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # Re-publishing without new stage should detect it's already published
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.publish()

        assert "Already published" in caplog.text


class TestPublishBaseKeptOnExternalChanges:
    def test_external_changes_prevent_base_advancement(self, topo: Topology, caplog):
        topo.commit_internal({"app.txt": "v1\n"})
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        # External contributor adds a file AFTER absorb — not yet absorbed
        topo.commit_to_public({"external.txt": "external contribution\n"})

        # Stage and publish without absorbing the external change first
        topo.commit_internal({"app.txt": "v2\n"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.stage_pr_branch, topo.cfg.internal_preview_branch)
        topo.work_dir.run("checkout", "main")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.publish()

        assert "Keeping publish base" in caplog.text
