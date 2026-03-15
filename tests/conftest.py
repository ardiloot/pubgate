import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from pubgate.config import Config
from pubgate.core import PubGate
from pubgate.git import GitRepo

# ---------------------------------------------------------------------------
# Test data constants (DRY)
# ---------------------------------------------------------------------------

# Minimal valid PNG header for binary file tests
SAMPLE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01"

# Latin-1 encoded text (valid Latin-1, invalid UTF-8) for encoding tests
SAMPLE_LATIN1 = b"caf\xe9 rese\xf1a\n"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: Path | str) -> str:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _git(cwd: Path | str, *args: str) -> str:
    return _run(["git", *args], cwd=cwd)


def _force_rmtree(path: Path) -> None:
    import os
    import shutil
    import stat
    import sys

    def _on_error(_func: object, fpath: str, exc: BaseException) -> None:
        os.chmod(fpath, stat.S_IWRITE)
        os.unlink(fpath)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_error)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: _on_error(f, p, e[1]))


# ---------------------------------------------------------------------------
# GitServer: simulates a bare git repository (hosting server)
# ---------------------------------------------------------------------------


class GitServer:
    def __init__(self, bare_path: Path) -> None:
        self.bare_path = bare_path

    def branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.bare_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def merge_branch(self, source: str, target: str, tmp_dir: Path) -> None:
        merge_clone = tmp_dir / f"merge-{uuid4().hex[:8]}"
        _git(tmp_dir, "clone", str(self.bare_path), str(merge_clone))
        _git(merge_clone, "checkout", target)
        _git(merge_clone, "merge", f"origin/{source}", "--no-ff", "-m", f"Merge {source} into {target}")
        _git(merge_clone, "push", "origin", target)
        _git(merge_clone, "push", "origin", "--delete", source)
        _force_rmtree(merge_clone)


# ---------------------------------------------------------------------------
# WorkDirectory: developer's local git clone
# ---------------------------------------------------------------------------


class WorkDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git = GitRepo(path)  # Production GitRepo for passing to PubGate

    def run(self, *args: str) -> str:
        return _git(self.path, *args)

    def commit_files(self, files: dict[str, str | bytes], msg: str) -> str:
        for fpath, content in files.items():
            full = self.path / fpath
            full.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                full.write_bytes(content)
            else:
                full.write_text(content, encoding="utf-8")
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-m", msg)
        return _git(self.path, "rev-parse", "HEAD").strip()

    def delete_files(self, paths: list[str], msg: str) -> str:
        for p in paths:
            full = self.path / p
            if full.exists():
                full.unlink()
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-m", msg)
        return _git(self.path, "rev-parse", "HEAD").strip()

    def push(self, remote: str, branch: str) -> None:
        _git(self.path, "push", remote, branch)

    def fetch(self, remote: str) -> None:
        _git(self.path, "fetch", "--prune", remote)

    def checkout(self, branch: str) -> None:
        _git(self.path, "checkout", branch)

    def reset_hard(self, ref: str) -> None:
        _git(self.path, "reset", "--hard", ref)

    def fetch_and_reset(self, remote: str, branch: str) -> None:
        self.fetch(remote)
        self.checkout(branch)
        self.reset_hard(f"{remote}/{branch}")

    def read_file_at_ref(self, ref: str, path: str) -> str | None:
        try:
            return _git(self.path, "show", f"{ref}:{path}")
        except RuntimeError:
            return None

    def list_files_at_ref(self, ref: str) -> list[str]:
        output = _git(self.path, "ls-tree", "-r", "--name-only", ref)
        return [line for line in output.strip().splitlines() if line]

    def is_worktree_clean(self) -> bool:
        result = _git(self.path, "status", "--porcelain")
        return result.strip() == ""


# ---------------------------------------------------------------------------
# Topology: full 4-repo test environment
# ---------------------------------------------------------------------------


class Topology:
    def __init__(
        self,
        internal_server: GitServer,
        public_server: GitServer,
        work_dir: WorkDirectory,
        external_contributor: WorkDirectory,
        tmp_dir: Path,
        cfg: Config,
    ) -> None:
        self.internal_server = internal_server
        self.public_server = public_server
        self.work_dir = work_dir
        self.external_contributor = external_contributor
        self.tmp_dir = tmp_dir
        self.cfg = cfg

    def commit_to_public(
        self,
        files: dict[str, str | bytes] | None = None,
        msg: str = "public commit",
        *,
        delete: list[str] | None = None,
    ) -> str:
        if delete:
            for p in delete:
                full = self.external_contributor.path / p
                if full.exists():
                    full.unlink()
        if files:
            self.external_contributor.commit_files(files, msg)
        else:
            self.external_contributor.run("add", "-A")
            self.external_contributor.run("commit", "-m", msg)
        sha = self.external_contributor.run("rev-parse", "HEAD").strip()
        self.external_contributor.push("origin", "main")
        return sha

    def rename_on_public(self, old: str, new: str, msg: str = "public rename") -> str:
        self.external_contributor.run("mv", old, new)
        self.external_contributor.run("commit", "-m", msg)
        sha = self.external_contributor.run("rev-parse", "HEAD").strip()
        self.external_contributor.push("origin", "main")
        return sha

    def commit_internal(self, files: dict[str, str | bytes], msg: str = "internal edit", *, push: bool = True) -> str:
        self.work_dir.run("checkout", "main")
        sha = self.work_dir.commit_files(files, msg)
        if push:
            self.work_dir.push("origin", "main")
        return sha

    def setup_baseline(self, filename: str, content: str) -> None:
        self.commit_to_public({filename: content})
        self.pubgate.absorb()
        self.merge_internal_pr(self.cfg.internal_absorb_branch, "main")

    def merge_internal_pr(self, branch: str, into: str) -> None:
        # Push target branch to origin if it doesn't exist (for orphan branches)
        if not self.internal_server.branch_exists(into):
            self.work_dir.push("origin", into)

        self.internal_server.merge_branch(branch, into, self.tmp_dir)
        self.work_dir.fetch_and_reset("origin", into)
        self.work_dir.run("checkout", "main")

    def merge_public_pr(self, branch: str, into: str) -> None:
        self.public_server.merge_branch(branch, into, self.tmp_dir)
        self.external_contributor.fetch_and_reset("origin", into)

    @property
    def pubgate(self) -> PubGate:
        return PubGate(self.cfg, self.work_dir.git)

    def bootstrap_absorb(self) -> None:
        self.pubgate.absorb()
        self.merge_internal_pr(self.cfg.internal_absorb_branch, "main")

    def stage_and_merge(self) -> None:
        self.bootstrap_absorb()
        self.pubgate.stage()
        self.merge_internal_pr(self.cfg.internal_stage_branch, self.cfg.internal_approved_branch)
        self.work_dir.run("checkout", "main")

    def publish_and_merge(self) -> None:
        self.pubgate.publish()
        self.work_dir.run("fetch", "public-remote")
        self.merge_public_pr(self.cfg.public_publish_branch, self.cfg.public_main_branch)
        self.work_dir.run("checkout", "main")

    def do_full_publish_cycle(self) -> None:
        self.pubgate.stage()
        self.merge_internal_pr(self.cfg.internal_stage_branch, self.cfg.internal_approved_branch)
        self.work_dir.run("checkout", "main")
        self.publish_and_merge()

    def absorb_and_merge(self) -> None:
        self.pubgate.absorb()
        self.merge_internal_pr(self.cfg.internal_absorb_branch, "main")


# ---------------------------------------------------------------------------
# Fixture: topo
# ---------------------------------------------------------------------------


@pytest.fixture()
def topo(tmp_path: Path) -> Topology:
    public_bare = tmp_path / "public.git"
    internal_bare = tmp_path / "internal.git"
    public_work = tmp_path / "public-work"
    work_dir_path = tmp_path / "work"

    # Create bare repos (servers)
    _git(tmp_path, "init", "--bare", str(public_bare))
    _git(tmp_path, "init", "--bare", str(internal_bare))

    # Seed public repo with initial commit
    _git(tmp_path, "clone", str(public_bare), str(public_work))
    _git(public_work, "checkout", "-b", "main")
    (public_work / "public-file.txt").write_text("public content\n", encoding="utf-8")
    _git(public_work, "add", ".")
    _git(public_work, "commit", "-m", "initial public commit")
    _git(public_work, "push", "-u", "origin", "main")

    # Clone internal and set up developer work directory
    _git(tmp_path, "clone", str(internal_bare), str(work_dir_path))
    _git(work_dir_path, "checkout", "-b", "main")
    (work_dir_path / "file1.txt").write_text("internal content\n", encoding="utf-8")
    _git(work_dir_path, "add", ".")
    _git(work_dir_path, "commit", "-m", "initial internal commit")
    _git(work_dir_path, "push", "-u", "origin", "main")

    # Add public repo as remote and fetch
    _git(work_dir_path, "remote", "add", "public-remote", str(public_bare))
    _git(work_dir_path, "fetch", "public-remote")

    return Topology(
        internal_server=GitServer(internal_bare),
        public_server=GitServer(public_bare),
        work_dir=WorkDirectory(work_dir_path),
        external_contributor=WorkDirectory(public_work),
        tmp_dir=tmp_path,
        cfg=Config(),
    )


@pytest.fixture()
def topo_empty_public(tmp_path: Path) -> Topology:
    public_bare = tmp_path / "public.git"
    internal_bare = tmp_path / "internal.git"
    public_work = tmp_path / "public-work"
    work_dir_path = tmp_path / "work"

    # Create bare repos (servers) - public stays empty
    _git(tmp_path, "init", "--bare", str(public_bare))
    _git(tmp_path, "init", "--bare", str(internal_bare))

    # Clone public but don't commit anything
    _git(tmp_path, "clone", str(public_bare), str(public_work))

    # Clone internal and set up developer work directory
    _git(tmp_path, "clone", str(internal_bare), str(work_dir_path))
    _git(work_dir_path, "checkout", "-b", "main")
    (work_dir_path / "file1.txt").write_text("internal content\n", encoding="utf-8")
    _git(work_dir_path, "add", ".")
    _git(work_dir_path, "commit", "-m", "initial internal commit")
    _git(work_dir_path, "push", "-u", "origin", "main")

    # Add public repo as remote and fetch
    _git(work_dir_path, "remote", "add", "public-remote", str(public_bare))
    _git(work_dir_path, "fetch", "public-remote")

    return Topology(
        internal_server=GitServer(internal_bare),
        public_server=GitServer(public_bare),
        work_dir=WorkDirectory(work_dir_path),
        external_contributor=WorkDirectory(public_work),
        tmp_dir=tmp_path,
        cfg=Config(),
    )
