import pytest

from pubgate.errors import PubGateError


class TestStartupBranchValidation:
    def test_error_when_not_on_main(self, topo):
        topo.work_dir.run("checkout", "-b", "other-branch")
        with pytest.raises(PubGateError, match="currently on 'other-branch'"):
            topo.pubgate.absorb()

    def test_error_when_detached_head(self, topo):
        head = topo.work_dir.run("rev-parse", "HEAD").strip()
        topo.work_dir.run("checkout", head)
        with pytest.raises(PubGateError, match="HEAD is detached"):
            topo.pubgate.absorb()


class TestStartupSyncValidation:
    def test_error_when_local_ahead(self, topo):
        topo.work_dir.commit_files({"extra.txt": "local only\n"}, "unpushed commit")
        with pytest.raises(PubGateError, match="1 unpushed commit"):
            topo.pubgate.absorb()

    def test_error_when_local_behind(self, topo):
        # Advance origin/main via the internal server side
        topo.commit_internal({"new.txt": "new\n"}, "advance origin", push=True)
        # Reset local back so it's behind
        topo.work_dir.run("reset", "--hard", "HEAD~1")
        with pytest.raises(PubGateError, match="behind"):
            topo.pubgate.absorb()

    def test_error_when_diverged(self, topo):
        # Push a commit via another path to advance origin
        topo.commit_internal({"server.txt": "server\n"}, "server commit", push=True)
        # Reset local back and make a different commit
        topo.work_dir.run("reset", "--hard", "HEAD~1")
        topo.work_dir.commit_files({"local.txt": "local\n"}, "local divergent commit")
        with pytest.raises(PubGateError, match="diverged.*1 ahead.*1 behind"):
            topo.pubgate.absorb()

    def test_success_when_synced(self, topo):
        # Normal case: local matches origin -- should not raise startup errors
        # (will proceed to bootstrap absorb since public has content)
        topo.pubgate.absorb()
