import pytest
from conftest import Topology

from pubgate.errors import GitError


class TestOnBranchCleanup:
    def test_worktree_clean_after_error_in_on_branch(self, topo: Topology):
        topo.work_dir.run("checkout", "main")

        # Create a branch to use as target
        topo.work_dir.run("branch", "test-branch", "main")

        with pytest.raises(RuntimeError, match="simulated"):
            with topo.work_dir.git.on_branch("test-branch"):
                # Stage a file, then blow up
                (topo.work_dir.path / "staged-junk.txt").write_text("junk\n", encoding="utf-8")
                topo.work_dir.run("add", "staged-junk.txt")
                raise RuntimeError("simulated failure")

        # Should be back on main with a clean worktree
        current = topo.work_dir.run("rev-parse", "--abbrev-ref", "HEAD").strip()
        assert current == "main"
        assert topo.work_dir.is_worktree_clean()

    def test_branch_restored_after_git_error(self, topo: Topology):
        topo.work_dir.run("checkout", "main")
        topo.work_dir.run("branch", "err-branch", "main")

        with pytest.raises(GitError):
            with topo.work_dir.git.on_branch("err-branch"):
                # Force a git error - try to commit with nothing staged
                topo.work_dir.git._run("commit", "-m", "empty")

        current = topo.work_dir.run("rev-parse", "--abbrev-ref", "HEAD").strip()
        assert current == "main"


class TestAbsorbRecovery:
    def test_rerun_after_absorb_succeeds(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_to_public({"recovery.txt": "test\n"})
        topo.pubgate.absorb()
        # Second run re-creates the branch (needs --force since PR wasn't merged)
        topo.pubgate.absorb(force=True)
