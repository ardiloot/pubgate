from unittest.mock import patch

import pytest
from conftest import Topology

from pubgate.errors import PubGateError
from pubgate.git import GitRepo, is_lfs_pointer


def _enable_lfs(topo: Topology) -> None:
    if not topo.work_dir.git.is_lfs_available():
        pytest.skip("Git LFS is not installed")
    topo.work_dir.run("lfs", "install", "--local")


class TestPreviewWorktreeLifecycle:
    def test_locked_worktree_can_be_discovered_reused_and_removed(self, topo: Topology):
        output = topo.tmp_dir / "preview"
        source_git = topo.work_dir.git
        base = source_git.rev_parse("HEAD")

        source_git.add_locked_worktree(output, base, "pubgate-preview-v1")
        try:
            info = source_git.find_worktree(output)
            assert info is not None
            assert info.lock_reason == "pubgate-preview-v1"
            assert GitRepo(output).current_branch() == "HEAD"

            preview_git = GitRepo(output)
            (output / "artifact.txt").write_text("artifact\n", encoding="utf-8")
            preview_git.clean_all()
            preview_git.reset_hard(base, skip_lfs_smudge=True)
            assert not (output / "artifact.txt").exists()
        finally:
            if source_git.find_worktree(output) is not None:
                source_git.remove_locked_worktree(output)

        assert source_git.find_worktree(output) is None


class TestPreviewWorkflow:
    def test_previews_clean_unpushed_feature_commit(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.work_dir.run("checkout", "-b", "feature")
        source_head = topo.work_dir.commit_files(
            {
                "feature.txt": "public feature\n",
                "mixed.py": "public\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\n",
            },
            "local feature",
        )
        source_status = topo.work_dir.run("status", "--porcelain")
        output = topo.tmp_dir / "preview"

        topo.pubgate.preview(output=output)

        preview_git = GitRepo(output)
        assert preview_git.current_branch() == "HEAD"
        assert (output / "feature.txt").read_text(encoding="utf-8") == "public feature\n"
        assert (output / "mixed.py").read_text(encoding="utf-8") == "public\n"
        assert (output / topo.cfg.stage_state_file).read_text(encoding="utf-8").strip() == source_head
        assert preview_git.has_staged_changes()
        assert not [line for line in preview_git.status_porcelain() if len(line) < 2 or line[1] != " "]
        assert topo.work_dir.git.rev_parse("HEAD") == source_head
        assert topo.work_dir.run("rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"
        assert topo.work_dir.run("status", "--porcelain") == source_status

        source_git = topo.work_dir.git
        source_git.remove_locked_worktree(output)

    def test_rejects_dirty_source(self, topo: Topology):
        (topo.work_dir.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with pytest.raises(PubGateError, match="working tree is not clean"):
            topo.pubgate.preview(output=topo.tmp_dir / "preview")

    def test_previews_detached_source_head(self, topo: Topology):
        source_head = topo.work_dir.git.rev_parse("HEAD")
        topo.work_dir.run("checkout", source_head)
        output = topo.tmp_dir / "preview"

        topo.pubgate.preview(output=output)

        assert topo.work_dir.git.current_branch() == "HEAD"
        assert topo.work_dir.git.rev_parse("HEAD") == source_head
        assert (output / topo.cfg.stage_state_file).read_text(encoding="utf-8").strip() == source_head
        topo.work_dir.git.remove_locked_worktree(output)

    def test_rejects_output_inside_or_around_source(self, topo: Topology):
        for output in (topo.work_dir.path / "preview", topo.tmp_dir):
            with pytest.raises(PubGateError, match="outside the source working tree"):
                topo.pubgate.preview(output=output)

    def test_rejects_sparse_checkout(self, topo: Topology):
        topo.work_dir.run("sparse-checkout", "init", "--cone")
        try:
            with pytest.raises(PubGateError, match="does not support sparse checkouts"):
                topo.pubgate.preview(output=topo.tmp_dir / "preview")
        finally:
            topo.work_dir.run("sparse-checkout", "disable")

    def test_works_before_approved_branch_exists(self, topo: Topology):
        output = topo.tmp_dir / "preview"
        source_head = topo.work_dir.git.rev_parse("HEAD")

        topo.pubgate.preview(output=output)

        preview_git = GitRepo(output)
        assert preview_git.ls_tree("HEAD") == []
        assert (output / "file1.txt").read_text(encoding="utf-8") == "internal content\n"
        assert (output / topo.cfg.stage_state_file).read_text(encoding="utf-8").strip() == source_head
        topo.work_dir.git.remove_locked_worktree(output)

    def test_force_reuses_worktree_and_removes_test_artifacts(self, topo: Topology):
        topo.stage_and_merge()
        topo.work_dir.run("checkout", "-b", "feature")
        topo.work_dir.commit_files(
            {
                ".gitignore": "build/\n",
                "feature.txt": "version 1\n",
            },
            "feature v1",
        )
        output = topo.tmp_dir / "preview"
        topo.pubgate.preview(output=output)
        preview_git = GitRepo(output)
        git_dir_before = preview_git._run("rev-parse", "--git-dir").stdout.strip()

        (output / "feature.txt").write_text("test modification\n", encoding="utf-8")
        (output / "untracked.txt").write_text("artifact\n", encoding="utf-8")
        (output / "build").mkdir()
        (output / "build" / "ignored.txt").write_text("artifact\n", encoding="utf-8")
        nested = output / "nested"
        nested.mkdir()
        preview_git._run("init", str(nested))

        topo.work_dir.commit_files({"feature.txt": "version 2\n"}, "feature v2")
        topo.pubgate.preview(output=output, force=True)

        assert GitRepo(output)._run("rev-parse", "--git-dir").stdout.strip() == git_dir_before
        assert (output / "feature.txt").read_text(encoding="utf-8") == "version 2\n"
        assert not (output / "untracked.txt").exists()
        assert not (output / "build").exists()
        assert not nested.exists()
        topo.work_dir.git.remove_locked_worktree(output)

    def test_filtering_failure_preserves_existing_preview(self, topo: Topology):
        output = topo.tmp_dir / "preview"
        topo.pubgate.preview(output=output)
        preview_git = GitRepo(output)
        tree_before = preview_git._run("write-tree").stdout.strip()

        topo.work_dir.commit_files({"broken.txt": "# BEGIN-INTERNAL\nsecret\n"}, "broken marker")
        with pytest.raises(PubGateError, match="unclosed BEGIN-INTERNAL"):
            topo.pubgate.preview(output=output, force=True)

        assert topo.work_dir.git.find_worktree(output) is not None
        assert GitRepo(output)._run("write-tree").stdout.strip() == tree_before
        topo.work_dir.git.remove_locked_worktree(output)

    def test_existing_preview_requires_force_and_is_preserved(self, topo: Topology):
        output = topo.tmp_dir / "preview"
        topo.pubgate.preview(output=output)
        tree_before = GitRepo(output)._run("write-tree").stdout.strip()

        with pytest.raises(PubGateError, match="already exists.*--force"):
            topo.pubgate.preview(output=output)

        assert GitRepo(output)._run("write-tree").stdout.strip() == tree_before
        topo.work_dir.git.remove_locked_worktree(output)

    def test_rejects_malformed_absorb_state(self, topo: Topology):
        topo.work_dir.commit_files({topo.cfg.absorb_state_file: "not-a-sha\n"}, "bad absorb state")
        output = topo.tmp_dir / "preview"

        with pytest.raises(PubGateError, match="invalid SHA"):
            topo.pubgate.preview(output=output)

        assert topo.work_dir.git.find_worktree(output) is None

    def test_force_rebases_preview_on_latest_approved(self, topo: Topology):
        topo.stage_and_merge()
        output = topo.tmp_dir / "preview"
        topo.pubgate.preview(output=output)
        old_base = GitRepo(output).rev_parse("HEAD")

        topo.commit_internal({"approved-v2.txt": "version 2\n"})
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        new_base = topo.work_dir.git.rev_parse(f"origin/{topo.cfg.internal_approved_branch}")
        assert new_base != old_base

        topo.pubgate.preview(output=output, force=True)

        assert GitRepo(output).rev_parse("HEAD") == new_base
        topo.work_dir.git.remove_locked_worktree(output)

    def test_force_refuses_unrelated_worktree(self, topo: Topology):
        output = topo.tmp_dir / "other-worktree"
        source_git = topo.work_dir.git
        source_git.add_locked_worktree(output, "HEAD", "unrelated")
        try:
            with pytest.raises(PubGateError, match="not a pubgate preview worktree"):
                topo.pubgate.preview(output=output, force=True)
        finally:
            source_git.remove_locked_worktree(output)

    def test_force_refuses_unrelated_directory(self, topo: Topology):
        output = topo.tmp_dir / "unrelated"
        output.mkdir()
        (output / "keep.txt").write_text("keep\n", encoding="utf-8")

        with pytest.raises(PubGateError, match="not a pubgate preview worktree"):
            topo.pubgate.preview(output=output, force=True)

        assert (output / "keep.txt").read_text(encoding="utf-8") == "keep\n"

    def test_generation_failure_removes_new_worktree(self, topo: Topology):
        output = topo.tmp_dir / "preview"

        with (
            patch("pubgate.core.apply_stage_snapshot", side_effect=RuntimeError("simulated failure")),
            pytest.raises(RuntimeError, match="simulated failure"),
        ):
            topo.pubgate.preview(output=output)

        assert topo.work_dir.git.find_worktree(output) is None
        assert not output.exists()

    def test_candidate_tree_matches_production_stage(self, topo: Topology):
        topo.bootstrap_absorb()
        topo.commit_internal(
            {
                "feature.txt": "public feature\n",
                "mixed.py": "public\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\n",
            }
        )
        output = topo.tmp_dir / "preview"

        topo.pubgate.preview(output=output)
        preview_tree = GitRepo(output)._run("write-tree").stdout.strip()
        topo.pubgate.stage()
        stage_tree = topo.work_dir.git.rev_parse(f"{topo.cfg.internal_stage_branch}^{{tree}}")

        assert preview_tree == stage_tree
        topo.work_dir.git.remove_locked_worktree(output)


class TestPreviewLfs:
    def test_materializes_locally_available_object(self, topo: Topology):
        _enable_lfs(topo)
        payload = b"preview lfs payload\x00\n"
        topo.work_dir.commit_files(
            {
                ".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
                "model.bin": payload,
            },
            "add lfs object",
        )
        assert is_lfs_pointer(topo.work_dir.git.read_file_at_ref_bytes("HEAD", "model.bin") or b"")
        output = topo.tmp_dir / "preview"

        topo.pubgate.preview(output=output)

        assert (output / "model.bin").read_bytes() == payload
        assert not [line for line in GitRepo(output).status_porcelain() if len(line) < 2 or line[1] != " "]
        topo.work_dir.git.remove_locked_worktree(output)

    def test_missing_object_remains_pointer(self, topo: Topology):
        _enable_lfs(topo)
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "size 12345\n"
        )
        topo.work_dir.commit_files(
            {
                ".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
                "missing.bin": pointer,
            },
            "add missing lfs pointer",
        )
        output = topo.tmp_dir / "preview"

        topo.pubgate.preview(output=output)

        assert (output / "missing.bin").read_text(encoding="utf-8") == pointer
        assert not [line for line in GitRepo(output).status_porcelain() if len(line) < 2 or line[1] != " "]
        topo.work_dir.git.remove_locked_worktree(output)
