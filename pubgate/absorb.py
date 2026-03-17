import logging
import tempfile
from pathlib import Path

from .config import Config
from .errors import GitError, PubGateError
from .filtering import scrub_internal_blocks
from .git import GitRepo, is_lfs_pointer
from .models import format_commit
from .state import AbsorbStatus, StateRef

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AbsorbResult:
    __slots__ = ("status", "public_head", "last_absorbed")

    def __init__(self, status: AbsorbStatus, public_head: str, last_absorbed: str | None) -> None:
        self.status = status
        self.public_head = public_head
        self.last_absorbed = last_absorbed


def check_absorb(cfg: Config, git: GitRepo) -> AbsorbResult:
    if not git.remote_branch_exists(cfg.public_remote, cfg.public_main_branch):
        raise PubGateError(
            f"Error: public repo has no '{cfg.public_main_branch}' branch. "
            f"The public repo must have at least one commit before running absorb."
        )
    public_head = git.rev_parse(cfg.public_main_ref)

    absorb_ref = StateRef.read(git, cfg.internal_main_branch, cfg.absorb_state_file)
    last_absorbed = absorb_ref.sha if absorb_ref else None

    if last_absorbed is None:
        logger.debug("Inbound status: NEEDS_BOOTSTRAP")
        return AbsorbResult(AbsorbStatus.NEEDS_BOOTSTRAP, public_head, None)

    if last_absorbed == public_head:
        logger.debug("Inbound status: UP_TO_DATE")
        return AbsorbResult(AbsorbStatus.UP_TO_DATE, public_head, last_absorbed)

    logger.debug("Inbound status: NEEDS_ABSORB")
    return AbsorbResult(AbsorbStatus.NEEDS_ABSORB, public_head, last_absorbed)


def resolve_and_apply(cfg: Config, git: GitRepo, base_sha: str, public_head: str) -> list[str]:
    public_ref = f"{cfg.public_remote}/{cfg.public_main_branch}"
    excluded = cfg.state_files
    staged_sha: str | None = None
    try:
        stage_ref = StateRef.read(git, public_ref, cfg.stage_state_file)
        if stage_ref is not None:
            staged_sha = stage_ref.sha
    except PubGateError as exc:
        logger.warning("Could not read stage state from %s: %s", public_ref, exc)
    return _apply_absorb_changes(git, base_sha, public_head, public_ref, excluded=excluded, staged_sha=staged_sha)


def absorb_commit_message(
    git: GitRepo,
    last_absorbed: str,
    public_head: str,
    conflicted: list[str] | None = None,
) -> str:
    subject = f"pubgate: absorb public changes {last_absorbed[:7]}..{public_head[:7]}"
    commits = git.log_oneline(last_absorbed, public_head)
    lines = [subject]
    if commits:
        lines.append("")
        lines.append(f"Included commits ({last_absorbed[:7]}..{public_head[:7]}):")
        lines.extend(f"  {i}. {format_commit(c)}" for i, c in enumerate(commits, 1))
    if conflicted:
        lines.append("")
        lines.append("CONFLICTS (resolve before merging):")
        for path in conflicted:
            lines.append(f"  {path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Implementation details (private)
# ---------------------------------------------------------------------------


def _read_text_at_ref(git: GitRepo, ref: str, path: str) -> str | None:
    data = git.read_file_at_ref_bytes(ref, path)
    if data is None:
        return None
    return data.decode("utf-8")


def _apply_absorb_changes(
    git: GitRepo,
    base_sha: str,
    public_head: str,
    public_ref: str,
    *,
    excluded: frozenset[str] = frozenset(),
    staged_sha: str | None = None,
) -> list[str]:
    changes = git.diff_tree(base_sha, public_head)
    changes = [c for c in changes if c.path not in excluded and (c.old_path is None or c.old_path not in excluded)]
    actions: list[str] = []

    for change in changes:
        logger.debug("Processing change: %s %s", change.status, change.path)
        if change.is_add:
            local_path = git.repo_dir / change.path
            if local_path.exists():
                kind = git.classify_at_ref(public_ref, change.path)
                if kind != "text":
                    label = "LFS file" if kind == "lfs" else "binary"
                    actions.append(f"  {label} added on public (kept local version, review manually): {change.path}")
                else:
                    theirs_content = _read_text_at_ref(git, public_ref, change.path)
                    if theirs_content is None:
                        actions.append(f"  added on public (kept local version, review manually): {change.path}")
                        continue
                    # Try to find the published base: the scrubbed version of
                    # the file at the internal commit that was staged.
                    published_base: str | None = None
                    if staged_sha is not None:
                        staged_content = _read_text_at_ref(git, staged_sha, change.path)
                        if staged_content is not None:
                            published_base = scrub_internal_blocks(staged_content, path=change.path)
                    if published_base is not None:
                        # Three-way merge using the published version as base
                        with tempfile.TemporaryDirectory() as tmpdir:
                            base_tmp = Path(tmpdir) / "base"
                            theirs_tmp = Path(tmpdir) / "theirs"
                            base_tmp.write_text(published_base, encoding="utf-8", newline="")
                            theirs_tmp.write_text(theirs_content, encoding="utf-8", newline="")
                            clean = git.merge_file(local_path, base_tmp, theirs_tmp)
                            git.stage(change.path)
                            if clean:
                                actions.append(f"  merge (clean): {change.path}")
                            else:
                                actions.append(f"  merge (CONFLICTS - resolve manually): {change.path}")
                    else:
                        # No staged version; file wasn't published through pubgate
                        actions.append(f"  added on public (kept local, review manually): {change.path}")
            else:
                is_binary = git.copy_file_from_ref(public_ref, change.path)
                if is_binary:
                    with open(git.repo_dir / change.path, "rb") as f:
                        head = f.read(1024)
                    tag = " (LFS)" if is_lfs_pointer(head) else " (binary)"
                else:
                    tag = ""
                actions.append(f"  add{tag}: {change.path}")

        elif change.is_modify:
            _merge_file(git, base_sha, public_ref, change.path, actions, staged_sha=staged_sha)

        elif change.is_delete:
            if (git.repo_dir / change.path).exists():
                actions.append(f"  deleted on public (kept locally, review manually): {change.path}")

        elif change.is_rename:
            old_path = change.old_path or ""
            git.copy_file_from_ref(public_ref, change.path)
            msg = f"  rename on public: {old_path} → {change.path} (kept old, review manually)"
            actions.append(msg)

    return actions


def _merge_file(
    git: GitRepo,
    base_sha: str,
    public_ref: str,
    path: str,
    actions: list[str],
    *,
    staged_sha: str | None = None,
) -> None:
    if git.is_binary_at_ref(public_ref, path) or git.is_binary_at_ref(base_sha, path):
        theirs_bytes = git.read_file_at_ref_bytes(public_ref, path)
        if theirs_bytes is None:
            raise GitError(
                ["show", f"{public_ref}:{path}"],
                1,
                f"diff_tree reported M for {path} but binary content is unreadable "
                f"at {public_ref}. Repository may have corrupt objects.",
            )
        git.write_file_and_stage_bytes(path, theirs_bytes)
        label = "LFS file" if is_lfs_pointer(theirs_bytes) else "binary"
        actions.append(f"  {label} changed on public (replaced locally, review manually): {path}")
        return

    # Use the scrubbed staged content as merge base when available.
    # This mirrors the is_add path: after a publish cycle, the correct base
    # is the scrubbed version of the internal file that was staged, not the
    # old public content (which would cause false conflicts on internal blocks).
    base_content: str | None = None
    if staged_sha is not None:
        staged_content = _read_text_at_ref(git, staged_sha, path)
        if staged_content is not None:
            base_content = scrub_internal_blocks(staged_content, path=path)
    if base_content is None:
        base_content = _read_text_at_ref(git, base_sha, path)
    theirs_content = _read_text_at_ref(git, public_ref, path)

    if base_content is None or theirs_content is None:
        missing_ref = base_sha if base_content is None else public_ref
        raise GitError(
            ["show", f"{missing_ref}:{path}"],
            1,
            f"diff_tree reported M for {path} but content is unreadable "
            f"at {missing_ref}. Repository may have corrupt objects.",
        )

    ours_path = git.repo_dir / path
    if not ours_path.exists():
        if theirs_content is not None:
            git.write_file_and_stage(path, theirs_content)
            actions.append(f"  add (was modified on public, missing locally): {path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        base_tmp = Path(tmpdir) / "base"
        theirs_tmp = Path(tmpdir) / "theirs"
        base_tmp.write_text(base_content, encoding="utf-8", newline="")
        theirs_tmp.write_text(theirs_content, encoding="utf-8", newline="")

        clean = git.merge_file(ours_path, base_tmp, theirs_tmp)
        git.stage(path)
        if clean:
            actions.append(f"  merge (clean): {path}")
        else:
            actions.append(f"  merge (CONFLICTS - resolve manually): {path}")
