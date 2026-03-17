import logging

from conftest import Topology


class TestStatusPreBootstrap:
    def test_fresh_repo_status(self, topo: Topology, caplog):
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Worktree: clean, on main, synced with origin" in caplog.text
        assert "Absorb (public → internal): not initialized" in caplog.text
        assert "→ run 'pubgate absorb' to bootstrap" in caplog.text
        assert "Stage (internal → review): blocked" in caplog.text
        assert "→ run 'pubgate absorb' first" in caplog.text
        assert "Publish (review → public): blocked" in caplog.text
        assert "→ run 'pubgate stage' and merge the PR first" in caplog.text


class TestStatusUpToDate:
    def test_absorb_up_to_date(self, topo: Topology, caplog):
        topo.bootstrap_absorb()

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Absorb (public → internal): up to date" in caplog.text

    def test_stage_up_to_date(self, topo: Topology, caplog):
        topo.stage_and_merge()

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Stage (internal → review): up to date" in caplog.text

    def test_publish_up_to_date(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Publish (review → public): up to date" in caplog.text

    def test_worktree_clean(self, topo: Topology, caplog):
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Worktree: clean, on main, synced with origin" in caplog.text


class TestStatusNeedsAbsorb:
    def test_new_public_commits_detected(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_to_public({"new.txt": "content"}, "Add new file")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Absorb (public → internal): 1 new commit" in caplog.text
        assert "Add new file" in caplog.text
        assert "→ run 'pubgate absorb'" in caplog.text

    def test_multiple_public_commits(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.commit_to_public({"a.txt": "a"}, "First change")
        topo.commit_to_public({"b.txt": "b"}, "Second change")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "2 new commits" in caplog.text
        assert "First change" in caplog.text
        assert "Second change" in caplog.text


class TestStatusNeedsStage:
    def test_internal_commits_since_last_stage(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.commit_internal({"extra.txt": "more"}, "Internal update")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Stage (internal → review): 1 commit since last stage" in caplog.text
        assert "Internal update" in caplog.text
        assert "→ run 'pubgate stage'" in caplog.text

    def test_not_yet_staged(self, topo: Topology, caplog):
        topo.bootstrap_absorb()

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Stage (internal → review): not yet staged" in caplog.text
        assert "→ run 'pubgate stage'" in caplog.text


class TestStatusNeedsPublish:
    def test_ready_to_publish(self, topo: Topology, caplog):
        topo.stage_and_merge()

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Publish (review → public): ready" in caplog.text
        assert "Last published: (none)" in caplog.text
        assert "→ run 'pubgate publish'" in caplog.text

    def test_new_stage_since_last_publish(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.publish_and_merge()
        topo.absorb_and_merge()
        topo.commit_internal({"extra.txt": "v2"}, "New feature")
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.work_dir.run("checkout", "main")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Publish (review → public): ready" in caplog.text
        assert "→ run 'pubgate publish'" in caplog.text


class TestStatusPRPending:
    def test_absorb_pr_pending(self, topo: Topology, caplog):
        topo.pubgate.absorb()
        # Don't merge - branch still on remote

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Absorb (public → internal): PR pending" in caplog.text
        assert "pubgate/absorb" in caplog.text

    def test_stage_pr_pending(self, topo: Topology, caplog):
        topo.bootstrap_absorb()
        topo.pubgate.stage()
        # Don't merge - branch still on remote

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Stage (internal → review): PR pending" in caplog.text
        assert "pubgate/stage" in caplog.text

    def test_publish_pr_pending(self, topo: Topology, caplog):
        topo.stage_and_merge()
        topo.pubgate.publish()
        # Don't merge the public PR - branch still on remote

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "Publish (review → public): PR pending" in caplog.text
        assert "pubgate/publish" in caplog.text


class TestStatusWorktree:
    def test_dirty_worktree(self, topo: Topology, caplog):
        (topo.work_dir.path / "uncommitted.txt").write_text("wip\n")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "dirty (uncommitted changes)" in caplog.text

    def test_wrong_branch(self, topo: Topology, caplog):
        topo.work_dir.run("checkout", "-b", "feature-x")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "on feature-x (expected main)" in caplog.text

    def test_unpushed_commits(self, topo: Topology, caplog):
        topo.work_dir.commit_files({"local.txt": "local"}, "Local only")
        # Don't push

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "1 unpushed commit(s)" in caplog.text

    def test_behind_origin(self, topo: Topology, caplog):
        # Push from another place to make origin ahead
        topo.work_dir.commit_files({"ahead.txt": "data"}, "Ahead commit")
        topo.work_dir.push("origin", "main")
        # Reset local back one commit
        topo.work_dir.run("reset", "--hard", "HEAD~1")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        assert "1 commit(s) behind origin" in caplog.text


class TestStatusNotPulled:
    def test_absorbed_and_merged_but_not_pulled(self, topo: Topology, caplog):
        topo.pubgate.absorb()
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, "main")
        # Don't pull — local main is behind origin/main
        topo.work_dir.run("reset", "--hard", "HEAD~1")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        # Should see absorb as up to date (reading from origin/main, not local)
        assert "Absorb (public → internal): up to date" in caplog.text
        assert "not initialized" not in caplog.text

    def test_staged_and_merged_but_not_pulled(self, topo: Topology, caplog):
        topo.stage_and_merge()
        # Add a new commit, stage it, merge — but don't pull
        topo.commit_internal({"new.txt": "content"}, "New file")
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.work_dir.run("checkout", "main")

        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()

        # Stage should reflect origin/main state, not stale local
        assert "Stage (internal → review): up to date" in caplog.text


class TestStatusEmptyPublic:
    def test_empty_public_repo(self, topo_empty_public: Topology, caplog):
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo_empty_public.pubgate.status()

        # Should report an error for absorb (no main branch)
        assert "Absorb (public → internal): error" in caplog.text


class TestStatusFullCycle:
    def test_status_through_full_workflow(self, topo: Topology, caplog):
        # 1. Fresh start
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()
        assert "not initialized" in caplog.text
        caplog.clear()

        # 2. After absorb + merge
        topo.bootstrap_absorb()
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()
        assert "Absorb (public → internal): up to date" in caplog.text
        assert "not yet staged" in caplog.text
        caplog.clear()

        # 3. After stage + merge
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.work_dir.run("checkout", "main")
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()
        assert "Stage (internal → review): up to date" in caplog.text
        assert "Publish (review → public): ready" in caplog.text
        caplog.clear()

        # 4. After publish + merge + absorb
        topo.publish_and_merge()
        topo.absorb_and_merge()
        with caplog.at_level(logging.DEBUG, logger="pubgate"):
            topo.pubgate.status()
        assert "Absorb (public → internal): up to date" in caplog.text
        # Stage reports absorb housekeeping commits (SHA comparison, not snapshot)
        assert "commits since last stage" in caplog.text
        assert "Publish (review → public): up to date" in caplog.text
