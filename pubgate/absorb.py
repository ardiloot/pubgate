import logging
import tempfile
from pathlib import Path

from .errors import GitError
from .filtering import scrub_internal_blocks
from .git import GitRepo

logger = logging.getLogger(__name__)


def apply_absorb_changes(
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
                if git.is_binary_at_ref(public_ref, change.path):
                    actions.append(f"  added on public (kept local version, review manually): {change.path}")
                else:
                    theirs_content = git.read_file_at_ref(public_ref, change.path)
                    if theirs_content is None:
                        actions.append(f"  added on public (kept local version, review manually): {change.path}")
                        continue
                    # Try to find the published base: the scrubbed version of
                    # the file at the internal commit that was staged.
                    published_base: str | None = None
                    if staged_sha is not None:
                        staged_content = git.read_file_at_ref(staged_sha, change.path)
                        if staged_content is not None:
                            published_base = scrub_internal_blocks(staged_content, path=change.path)
                    if published_base is not None:
                        # Three-way merge using the published version as base
                        with tempfile.TemporaryDirectory() as tmpdir:
                            base_tmp = Path(tmpdir) / "base"
                            theirs_tmp = Path(tmpdir) / "theirs"
                            base_tmp.write_text(published_base, encoding="utf-8")
                            theirs_tmp.write_text(theirs_content, encoding="utf-8")
                            clean = git.merge_file(local_path, base_tmp, theirs_tmp)
                            git.stage(change.path)
                            if clean:
                                actions.append(f"  merge (clean): {change.path}")
                            else:
                                actions.append(f"  merge (CONFLICTS - resolve manually): {change.path}")
                    else:
                        # No staged version — file wasn't published through pubgate
                        actions.append(f"  added on public (kept local, review manually): {change.path}")
            else:
                is_binary = git.copy_file_from_ref(public_ref, change.path)
                actions.append(f"  add{' (binary)' if is_binary else ''}: {change.path}")

        elif change.is_modify:
            _merge_file(git, base_sha, public_ref, change.path, actions)

        elif change.is_delete:
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
) -> None:
    if git.is_binary_at_ref(public_ref, path) or git.is_binary_at_ref(base_sha, path):
        theirs_bytes = git.read_file_at_ref_bytes(public_ref, path)
        if theirs_bytes is not None:
            git.write_file_and_stage_bytes(path, theirs_bytes)
            actions.append(f"  binary changed on public (replaced locally, review manually): {path}")
        return

    base_content = git.read_file_at_ref(base_sha, path)
    theirs_content = git.read_file_at_ref(public_ref, path)

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
        base_tmp.write_text(base_content, encoding="utf-8")
        theirs_tmp.write_text(theirs_content, encoding="utf-8")

        clean = git.merge_file(ours_path, base_tmp, theirs_tmp)
        git.stage(path)
        if clean:
            actions.append(f"  merge (clean): {path}")
        else:
            actions.append(f"  merge (CONFLICTS - resolve manually): {path}")
