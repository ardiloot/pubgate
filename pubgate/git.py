import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import GitError, PubGateError
from .models import CommitInfo, FileChange

logger = logging.getLogger(__name__)

_TIMEOUT_LOCAL = 60
_TIMEOUT_NETWORK = 300


_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_LFS_POINTER_MAX_LEN = 512


def is_lfs_pointer(data: str | bytes) -> bool:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > _LFS_POINTER_MAX_LEN:
        return False
    if not data.startswith(_LFS_POINTER_PREFIX):
        return False
    return b"\noid sha256:" in data and b"\nsize " in data


class GitRepo:
    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir
        self._lfs_available: bool | None = None

    # ------------------------------------------------------------------
    # Internal runners
    # ------------------------------------------------------------------

    def _run(
        self,
        *args: str,
        check: bool = True,
        timeout: int = _TIMEOUT_LOCAL,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-C", str(self.repo_dir), *args]
        logger.debug("git %s", " ".join(args))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
        except subprocess.TimeoutExpired as exc:
            raise GitError(list(args), -1, f"timed out after {timeout}s") from exc
        logger.debug("git exit=%d", result.returncode)
        if result.stderr.strip() and (check or result.returncode == 0):
            logger.debug("git stderr: %s", result.stderr.strip())
        if check and result.returncode != 0:
            raise GitError(list(args), result.returncode, result.stderr.strip())
        return result

    def _run_bytes(
        self,
        *args: str,
        check: bool = True,
        timeout: int = _TIMEOUT_LOCAL,
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = ["git", "-C", str(self.repo_dir), *args]
        logger.debug("git %s", " ".join(args))
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)  # noqa: S603
        except subprocess.TimeoutExpired as exc:
            raise GitError(list(args), -1, f"timed out after {timeout}s") from exc
        logger.debug("git exit=%d", result.returncode)
        stderr_text = result.stderr.decode(errors="replace").strip()
        if stderr_text and (check or result.returncode == 0):
            logger.debug("git stderr: %s", stderr_text)
        if check and result.returncode != 0:
            raise GitError(list(args), result.returncode, stderr_text)
        return result

    # ------------------------------------------------------------------
    # Repository validation
    # ------------------------------------------------------------------

    def verify_repo(self) -> None:
        cmd = ["git", "-C", str(self.repo_dir), "rev-parse", "--git-dir"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_LOCAL)  # noqa: S603
        except subprocess.TimeoutExpired as exc:
            raise PubGateError(f"Error: timed out checking if '{self.repo_dir}' is a git repository.") from exc
        if result.returncode != 0:
            raise PubGateError(f"Error: '{self.repo_dir}' is not a git repository.")

    # ------------------------------------------------------------------
    # Remote operations
    # ------------------------------------------------------------------

    def get_remote_url(self, name: str) -> str:
        result = self._run("remote", "get-url", name)
        return result.stdout.strip()

    def ensure_remote(self, name: str, url: str | None) -> None:
        result = self._run("remote", "get-url", name, check=False)
        remote_exists = result.returncode == 0
        current_url = result.stdout.strip() if remote_exists else ""

        if not remote_exists:
            if not url:
                raise PubGateError(
                    f"Error: git remote '{name}' does not exist and no public_url is set in pubgate.toml."
                )
            self._run("remote", "add", name, url)
        elif url and current_url != url:
            self._run("remote", "set-url", name, url)

    def remote_branch_exists(self, remote: str, branch: str) -> bool:
        ref = f"refs/remotes/{remote}/{branch}"
        result = self._run("rev-parse", "--verify", ref, check=False)
        return result.returncode == 0

    def fetch(self, remote: str) -> None:
        self._run("fetch", "--prune", remote, timeout=_TIMEOUT_NETWORK)

    def ensure_branch_synced(self, branch: str, remote: str, remote_branch: str) -> None:
        local_sha = self.rev_parse(branch)
        remote_ref = f"{remote}/{remote_branch}"
        remote_sha = self.rev_parse(remote_ref)
        if local_sha == remote_sha:
            return
        ahead = len(self.rev_list(remote_sha, branch))
        behind = len(self.rev_list(local_sha, remote_ref))
        if ahead and behind:
            raise PubGateError(
                f"Error: local '{branch}' has diverged from {remote_ref} "
                f"({ahead} ahead, {behind} behind). Reconcile manually."
            )
        if ahead:
            raise PubGateError(
                f"Error: local '{branch}' has {ahead} unpushed commit(s) vs {remote_ref}. Push or reset first."
            )
        raise PubGateError(
            f"Error: local '{branch}' is {behind} commit(s) behind {remote_ref}. Run 'git pull --rebase' first."
        )

    def push(self, local_branch: str, remote: str, remote_branch: str, *, force: bool = False) -> None:
        args = ["push"]
        if force:
            args.append("--force")
        args += [remote, f"{local_branch}:{remote_branch}"]
        self._run(*args, timeout=_TIMEOUT_NETWORK)

    # ------------------------------------------------------------------
    # Branch operations
    # ------------------------------------------------------------------

    def branch_exists(self, name: str) -> bool:
        result = self._run("rev-parse", "--verify", f"refs/heads/{name}", check=False)
        return result.returncode == 0

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def create_or_update_branch(self, name: str, start_point: str) -> None:
        self._run("branch", "-f", name, start_point)

    def delete_branch(self, name: str) -> None:
        if self.branch_exists(name):
            self._run("branch", "-D", name)

    def delete_branch_safe(self, name: str) -> bool:
        result = self._run("branch", "-D", name, check=False)
        return result.returncode == 0

    # ------------------------------------------------------------------
    # Checkout operations
    # ------------------------------------------------------------------

    def checkout(self, branch: str) -> None:
        self._run("checkout", branch)

    def checkout_safe(self, branch: str) -> bool:
        result = self._run("checkout", branch, check=False)
        return result.returncode == 0

    def checkout_orphan(self, branch: str) -> None:
        self._run("checkout", "--orphan", branch)

    @contextmanager
    def on_branch(self, branch: str) -> Iterator[None]:
        original = self.current_branch()
        if original == "HEAD":
            raise PubGateError("Error: cannot run on a detached HEAD. Check out a branch first.")
        self.checkout(branch)
        try:
            yield
        except BaseException:
            try:
                self._run("reset", "HEAD", check=False)
                self._run("checkout", "--", ".", check=False)
                self._run("clean", "-fd", check=False)
            except Exception as cleanup_exc:
                logger.error(
                    "Failed to clean up worktree after error: %s. "
                    "The worktree may be in a dirty state. "
                    "Run 'git checkout -- . && git clean -fd' to recover.",
                    cleanup_exc,
                )
            raise
        finally:
            self.checkout(original)

    # ------------------------------------------------------------------
    # Worktree state
    # ------------------------------------------------------------------

    def ensure_clean_worktree(self) -> None:
        result = self._run("status", "--porcelain")
        if result.stdout.strip():
            raise PubGateError("Error: working tree is not clean. Commit or stash changes first.")

    # ------------------------------------------------------------------
    # Ref & commit history
    # ------------------------------------------------------------------

    def rev_parse(self, ref: str) -> str:
        return self._run("rev-parse", ref).stdout.strip()

    def rev_list(self, base: str, head: str, *, first_parent: bool = False) -> list[str]:
        args = ["rev-list"]
        if first_parent:
            args.append("--first-parent")
        args.append(f"{base}..{head}")
        result = self._run(*args)
        return [c for c in result.stdout.strip().splitlines() if c]

    def log_oneline(self, base: str, head: str) -> list[CommitInfo]:
        _SEP = "\x1f"  # ASCII unit separator
        result = self._run("log", "--reverse", f"--format=%H{_SEP}%s{_SEP}%aN{_SEP}%ai", f"{base}..{head}")
        entries = []
        for line in result.stdout.strip().splitlines():
            if line:
                parts = line.split(_SEP, 3)
                # %ai gives "YYYY-MM-DD HH:MM:SS +ZZZZ", keep "YYYY-MM-DD HH:MM"
                date_str = parts[3].rsplit(":", 1)[0] if parts[3] else parts[3]
                entries.append(CommitInfo(sha=parts[0], subject=parts[1], author=parts[2], date=date_str))
        return entries

    def find_commit_introducing(self, base: str, head: str, path: str, content: str) -> str | None:
        result = self._run(
            "log",
            "--reverse",
            "--format=%H",
            f"-S{content}",
            f"{base}..{head}",
            "--",
            path,
        )
        first_line = result.stdout.strip().split("\n", 1)[0]
        return first_line if first_line else None

    def changed_files_in_commit(self, sha: str) -> list[str]:
        result = self._run("diff-tree", "--no-commit-id", "-r", "--name-only", f"{sha}~1", sha)
        return [f for f in result.stdout.strip().splitlines() if f]

    # ------------------------------------------------------------------
    # Tree & diff
    # ------------------------------------------------------------------

    def ls_tree(self, ref: str) -> list[str]:
        result = self._run_bytes("ls-tree", "-r", "--name-only", "-z", ref)
        return [p.decode("utf-8", errors="surrogateescape") for p in result.stdout.split(b"\x00") if p]

    def diff_tree(self, ref_a: str, ref_b: str) -> list[FileChange]:
        result = self._run_bytes("diff-tree", "-r", "--no-commit-id", "--name-status", "-z", ref_a, ref_b)
        changes: list[FileChange] = []
        parts = result.stdout.split(b"\x00")
        i = 0
        while i < len(parts):
            part = parts[i]
            if not part:
                i += 1
                continue
            status = chr(part[0])
            if status == "R" and i + 2 < len(parts):
                old_path = parts[i + 1].decode("utf-8", errors="surrogateescape")
                new_path = parts[i + 2].decode("utf-8", errors="surrogateescape")
                changes.append(FileChange(status=status, path=new_path, old_path=old_path))
                i += 3
            else:
                if i + 1 < len(parts):
                    path = parts[i + 1].decode("utf-8", errors="surrogateescape")
                    changes.append(FileChange(status=status, path=path))
                    i += 2
                else:
                    i += 1
        return changes

    # ------------------------------------------------------------------
    # File reading at ref
    # ------------------------------------------------------------------

    def read_file_at_ref(self, ref: str, path: str) -> str | None:
        result = self._run("show", f"{ref}:{path}", check=False)
        if result.returncode != 0:
            if (
                "does not exist" in result.stderr
                or "invalid object name" in result.stderr
                or "exists on disk, but not in" in result.stderr
            ):
                return None
            raise GitError(["show", f"{ref}:{path}"], result.returncode, result.stderr.strip())
        return result.stdout

    def read_file_at_ref_bytes(self, ref: str, path: str) -> bytes | None:
        result = self._run_bytes("show", f"{ref}:{path}", check=False)
        if result.returncode != 0:
            if (
                b"does not exist" in result.stderr
                or b"invalid object name" in result.stderr
                or b"exists on disk, but not in" in result.stderr
            ):
                return None
            raise GitError(
                ["show", f"{ref}:{path}"],
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
        return result.stdout

    def classify_at_ref(self, ref: str, path: str) -> str:
        data = self.read_file_at_ref_bytes(ref, path)
        if data is None:
            return "text"
        if is_lfs_pointer(data):
            return "lfs"
        chunk = data[:8192]
        if b"\x00" in chunk:
            return "binary"
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return "binary"
        return "text"

    def is_binary_at_ref(self, ref: str, path: str) -> bool:
        return self.classify_at_ref(ref, path) != "text"

    def read_file_auto(self, ref: str, path: str) -> str | bytes | None:
        data = self.read_file_at_ref_bytes(ref, path)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return data

    # ------------------------------------------------------------------
    # Index & staging
    # ------------------------------------------------------------------

    def has_staged_changes(self) -> bool:
        result = self._run("diff", "--cached", "--quiet", check=False)
        return result.returncode != 0

    def stage(self, path: str) -> None:
        self._run("add", path)

    def write_file_and_stage(self, repo_relative_path: str, content: str) -> None:
        full_path = self.repo_dir / repo_relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8", newline="")
        self._run("add", repo_relative_path)

    def write_file_and_stage_bytes(self, repo_relative_path: str, content: bytes) -> None:
        full_path = self.repo_dir / repo_relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)
        self._run("add", repo_relative_path)

    def write_file_and_stage_auto(self, path: str, content: str | bytes) -> None:
        if isinstance(content, bytes):
            self.write_file_and_stage_bytes(path, content)
        else:
            self.write_file_and_stage(path, content)

    def remove_file_and_stage(self, repo_relative_path: str) -> None:
        full_path = self.repo_dir / repo_relative_path
        if full_path.exists():
            full_path.unlink()
        self._run("rm", "--cached", "--ignore-unmatch", repo_relative_path)

    def rm_all_tracked(self) -> None:
        self._run("rm", "-rf", "--ignore-unmatch", ".", check=False)

    def copy_file_from_ref(self, ref: str, path: str) -> bool:
        content = self.read_file_auto(ref, path)
        if content is None:
            logger.warning("Could not read file %s at %s (skipped)", path, ref)
            return False
        is_binary = isinstance(content, bytes) or is_lfs_pointer(content)
        self.write_file_and_stage_auto(path, content)
        return is_binary

    # ------------------------------------------------------------------
    # Commits
    # ------------------------------------------------------------------

    def commit(self, message: str) -> str:
        self._run("commit", "--no-verify", "-m", message)
        return self.rev_parse("HEAD")

    def commit_allow_empty(self, message: str) -> str:
        self._run("commit", "--allow-empty", "--no-verify", "-m", message)
        return self.rev_parse("HEAD")

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    def merge_file(self, ours: Path, base: Path, theirs: Path) -> bool:
        result = self._run("merge-file", str(ours), str(base), str(theirs), check=False)
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return result.returncode == 0

    # ------------------------------------------------------------------
    # LFS operations
    # ------------------------------------------------------------------

    def is_lfs_available(self) -> bool:
        if self._lfs_available is None:
            result = self._run("lfs", "version", check=False)
            self._lfs_available = result.returncode == 0
            if self._lfs_available:
                logger.debug("Git LFS available: %s", result.stdout.strip())
            else:
                logger.debug("Git LFS not available")
        return self._lfs_available

    def lfs_fetch(self, remote: str, ref: str) -> None:
        if not self.is_lfs_available():
            return
        logger.info("Fetching LFS objects from %s for %s", remote, ref)
        result = self._run("lfs", "fetch", remote, ref, check=False, timeout=_TIMEOUT_NETWORK)
        if result.returncode != 0:
            logger.warning("LFS fetch failed (exit %d): %s", result.returncode, result.stderr.strip())

    def lfs_push(self, remote: str, branch: str) -> None:
        if not self.is_lfs_available():
            return
        logger.info("Pushing LFS objects to %s for %s", remote, branch)
        result = self._run("lfs", "push", remote, branch, check=False, timeout=_TIMEOUT_NETWORK)
        if result.returncode != 0:
            logger.warning("LFS push failed (exit %d): %s", result.returncode, result.stderr.strip())
