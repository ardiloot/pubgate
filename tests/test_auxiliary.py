import logging
from pathlib import Path

import pytest
from conftest import Topology

from pubgate.auxiliary import AuxiliaryFile, build_auxiliary_files, copy_auxiliary_files
from pubgate.config import AuxiliaryDir, Config, load_config
from pubgate.errors import PubGateError
from pubgate.git import GitRepo, is_lfs_pointer


class TestAuxiliaryConfig:
    def test_loads_simple_and_filtered_mappings(self, tmp_path: Path):
        absolute_source = tmp_path / "shared-data"
        (tmp_path / "pubgate.toml").write_text(
            f"""
[[auxiliary_dirs]]
source = "../models"
destination = "data/models"

[[auxiliary_dirs]]
source = "{absolute_source.as_posix()}"
destination = "data/shared"
include = ["*.bin"]
exclude = ["*.internal.bin"]
""".strip(),
            encoding="utf-8",
        )

        cfg = load_config(tmp_path)

        assert cfg.auxiliary_dirs == [
            AuxiliaryDir(source=(tmp_path / "../models").resolve(), destination="data/models"),
            AuxiliaryDir(
                source=absolute_source,
                destination="data/shared",
                include=("*.bin",),
                exclude=("*.internal.bin",),
            ),
        ]

    @pytest.mark.parametrize(
        "destination",
        ["", ".", "../data", "/data", "data\\models", ".git/data", "data/.git", "pubgate.toml/data"],
    )
    def test_rejects_unsafe_destination(self, tmp_path: Path, destination: str):
        destination = destination.replace("\\", "\\\\")
        (tmp_path / "pubgate.toml").write_text(
            f'[[auxiliary_dirs]]\nsource = "../models"\ndestination = "{destination}"\n',
            encoding="utf-8",
        )

        with pytest.raises(PubGateError):
            load_config(tmp_path)

    def test_rejects_overlapping_destinations(self, tmp_path: Path):
        (tmp_path / "pubgate.toml").write_text(
            """
[[auxiliary_dirs]]
source = "../models"
destination = "data"

[[auxiliary_dirs]]
source = "../other"
destination = "data/other"
""".strip(),
            encoding="utf-8",
        )

        with pytest.raises(PubGateError, match="destinations overlap"):
            load_config(tmp_path)


class TestBuildAuxiliaryFiles:
    def test_whole_tree_and_filters_use_existing_pattern_semantics(self, tmp_path: Path):
        source = tmp_path / "source"
        (source / "nested").mkdir(parents=True)
        (source / "root.txt").write_text("root", encoding="utf-8")
        (source / "nested" / "keep.bin").write_bytes(b"keep")
        (source / "nested" / "skip.internal.bin").write_bytes(b"skip")
        mappings = [
            AuxiliaryDir(source=source, destination="all"),
            AuxiliaryDir(
                source=source,
                destination="selected",
                include=("*.bin",),
                exclude=("*.internal.bin",),
            ),
        ]

        files = build_auxiliary_files(mappings, internal_paths=set())

        assert [file.destination_path for file in files] == [
            "all/nested/keep.bin",
            "all/nested/skip.internal.bin",
            "all/root.txt",
            "selected/nested/keep.bin",
        ]

    def test_rejects_missing_source(self, tmp_path: Path):
        mapping = AuxiliaryDir(source=tmp_path / "missing", destination="data")

        with pytest.raises(PubGateError, match="not a readable directory"):
            build_auxiliary_files([mapping], internal_paths=set())

    def test_rejects_internal_collision(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "model.bin").write_bytes(b"model")

        with pytest.raises(PubGateError, match="collides with internal snapshot"):
            build_auxiliary_files(
                [AuxiliaryDir(source=source, destination="data/models")],
                internal_paths={"data/models/readme.txt"},
            )

    def test_rejects_selected_symlink(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        target = source / "target.bin"
        target.write_bytes(b"model")
        link = source / "link.bin"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are unavailable")

        with pytest.raises(PubGateError, match="symlink"):
            build_auxiliary_files([AuxiliaryDir(source=source, destination="data")], internal_paths=set())

    def test_rejects_symlinked_source_root(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        link = tmp_path / "source-link"
        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")

        with pytest.raises(PubGateError, match="must not contain symlinks"):
            build_auxiliary_files([AuxiliaryDir(source=link, destination="data")], internal_paths=set())

    def test_rejects_symlink_in_source_parent(self, tmp_path: Path):
        source_parent = tmp_path / "source-parent"
        source = source_parent / "nested"
        source.mkdir(parents=True)
        link = tmp_path / "parent-link"
        try:
            link.symlink_to(source_parent, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")

        with pytest.raises(PubGateError, match="must not contain symlinks"):
            build_auxiliary_files(
                [AuxiliaryDir(source=link / "nested", destination="data")],
                internal_paths=set(),
            )


class TestCopyAuxiliaryFiles:
    def test_wraps_destination_preparation_error(self, tmp_path: Path):
        source = tmp_path / "source.bin"
        source.write_bytes(b"data")
        target = tmp_path / "target"
        target.mkdir()
        (target / "data").write_text("blocking file", encoding="utf-8")

        with pytest.raises(PubGateError, match="cannot prepare auxiliary destination"):
            copy_auxiliary_files(target, [AuxiliaryFile(source, "data/source.bin")])


class TestAuxiliaryStage:
    def test_preview_tree_matches_stage_tree(self, topo: Topology, caplog):
        source = topo.tmp_dir / "models"
        source.mkdir()
        (source / "model.bin").write_bytes(b"model")
        (source / "skip.tmp").write_bytes(b"skip")
        topo.cfg.auxiliary_dirs = [AuxiliaryDir(source=source, destination="data/models", exclude=("*.tmp",))]
        topo.bootstrap_absorb()
        output = topo.tmp_dir / "preview"

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.preview(output=output)
        assert f"Copying auxiliary directory {source} -> data/models (1 file)" in caplog.text
        preview_tree = GitRepo(output)._run("write-tree").stdout.strip()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.stage()
        assert f"Copying auxiliary directory {source} -> data/models (1 file)" in caplog.text
        stage_tree = topo.work_dir.git.rev_parse(f"{topo.cfg.internal_stage_branch}^{{tree}}")

        assert preview_tree == stage_tree
        topo.work_dir.git.remove_locked_worktree(output)

    def test_stages_auxiliary_only_update_without_new_internal_commit(self, topo: Topology):
        source = topo.tmp_dir / "models"
        source.mkdir()
        model = source / "model.bin"
        model.write_bytes(b"version 1")
        topo.cfg.auxiliary_dirs = [AuxiliaryDir(source=source, destination="data/models")]

        topo.bootstrap_absorb()
        internal_head = topo.work_dir.git.rev_parse("main")
        topo.pubgate.stage()
        assert (
            topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "data/models/model.bin")
            == b"version 1"
        )
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)

        model.write_bytes(b"version 2")
        topo.pubgate.stage()

        assert topo.work_dir.git.rev_parse("main") == internal_head
        assert (
            topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "data/models/model.bin")
            == b"version 2"
        )
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)

        model.unlink()
        topo.pubgate.stage()

        assert topo.work_dir.git.read_file_at_ref_bytes(topo.cfg.internal_stage_branch, "data/models/model.bin") is None

    def test_unchanged_auxiliary_is_noop_and_status_is_explicit(self, topo: Topology, caplog):
        source = topo.tmp_dir / "models"
        source.mkdir()
        (source / "model.bin").write_bytes(b"model")
        topo.cfg.auxiliary_dirs = [AuxiliaryDir(source=source, destination="data/models")]
        topo.stage_and_merge()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.status()
        assert "auxiliary sources not checked" in caplog.text

        topo.pubgate.stage()
        assert not topo.work_dir.git.remote_branch_exists("origin", topo.cfg.internal_stage_branch)
        topo.pubgate.stage()
        assert not topo.work_dir.git.remote_branch_exists("origin", topo.cfg.internal_stage_branch)

    def test_auxiliary_only_update_is_ready_to_publish(self, topo: Topology, caplog):
        source = topo.tmp_dir / "models"
        source.mkdir()
        model = source / "model.bin"
        model.write_bytes(b"version 1")
        topo.commit_internal(
            {"pubgate.toml": (f'[[auxiliary_dirs]]\nsource = "{source.as_posix()}"\ndestination = "data/models"\n')},
            "add auxiliary mapping",
        )
        topo.cfg = load_config(topo.work_dir.path)

        topo.stage_and_merge()
        topo.publish_and_merge()

        model.write_bytes(b"version 2")
        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.cfg = Config()

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.status()
        assert "Publish (review → public): ready (auxiliary files changed)" in caplog.text

        topo.publish()
        topo.work_dir.run("fetch", "public-remote")
        assert (
            topo.work_dir.git.read_file_at_ref_bytes(
                f"public-remote/{topo.cfg.public_publish_branch}",
                "data/models/model.bin",
            )
            == b"version 2"
        )


class TestAuxiliaryAbsorb:
    def test_reports_and_ignores_public_auxiliary_change(self, topo: Topology, caplog):
        source = topo.tmp_dir / "models"
        source.mkdir()
        (source / "model.bin").write_bytes(b"internal auxiliary")
        topo.cfg.auxiliary_dirs = [AuxiliaryDir(source=source, destination="data/models")]
        topo.stage_and_merge()
        topo.publish_and_merge()

        topo.commit_to_public({"data/models/model.bin": b"public edit"}, "edit auxiliary data")
        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()

        assert "Ignoring 1 public auxiliary path" in caplog.text
        assert "data/models/model.bin" in caplog.text
        assert (
            topo.work_dir.git.read_file_at_ref_bytes(
                topo.cfg.internal_absorb_branch,
                "data/models/model.bin",
            )
            is None
        )

    def test_mapping_removal_keeps_ownership_until_published(self, topo: Topology, caplog):
        source = topo.tmp_dir / "models"
        source.mkdir()
        (source / "model.bin").write_bytes(b"model")
        topo.commit_internal(
            {"pubgate.toml": (f'[[auxiliary_dirs]]\nsource = "{source.as_posix()}"\ndestination = "data/models"\n')},
            "add auxiliary mapping",
        )
        topo.cfg = load_config(topo.work_dir.path)
        topo.stage_and_merge()
        topo.publish_and_merge()

        topo.commit_internal({"pubgate.toml": "# no auxiliary mappings\n"}, "remove auxiliary mapping")
        topo.cfg = load_config(topo.work_dir.path)
        topo.commit_to_public({"data/models/model.bin": b"public edit"}, "edit removed mapping")

        with caplog.at_level(logging.INFO, logger="pubgate"):
            topo.pubgate.absorb()
        assert "Ignoring 1 public auxiliary path" in caplog.text
        topo.merge_internal_pr(topo.cfg.internal_absorb_branch, topo.cfg.internal_main_branch)

        topo.pubgate.stage()
        topo.merge_internal_pr(topo.cfg.internal_stage_branch, topo.cfg.internal_approved_branch)
        topo.publish_and_merge()

        assert not (topo.external_contributor.path / "data" / "models" / "model.bin").exists()


class TestAuxiliaryLfs:
    def test_raw_auxiliary_file_uses_repository_lfs_attributes(self, topo: Topology):
        if not topo.work_dir.git.is_lfs_available():
            pytest.skip("Git LFS is not installed")
        topo.work_dir.run("lfs", "install", "--local")
        topo.external_contributor.run("lfs", "install", "--local")

        payload = b"auxiliary lfs payload\x00\n"
        source = topo.tmp_dir / "models"
        source.mkdir()
        (source / "model.bin").write_bytes(payload)
        topo.cfg.auxiliary_dirs = [AuxiliaryDir(source=source, destination="data/models")]
        topo.commit_internal(
            {
                ".gitattributes": "data/models/*.bin filter=lfs diff=lfs merge=lfs -text\n",
            }
        )

        output = topo.tmp_dir / "preview"
        topo.pubgate.preview(output=output)
        assert (output / "data" / "models" / "model.bin").read_bytes() == payload
        topo.work_dir.git.remove_locked_worktree(output)

        topo.stage_and_merge()
        approved_ref = f"origin/{topo.cfg.internal_approved_branch}"
        pointer = topo.work_dir.git.read_file_at_ref_bytes(approved_ref, "data/models/model.bin")
        assert pointer is not None and is_lfs_pointer(pointer)

        topo.publish_and_merge()
        assert (topo.external_contributor.path / "data" / "models" / "model.bin").read_bytes() == payload
