import logging
from collections.abc import Sequence

from .auxiliary import AuxiliaryFile, copy_auxiliary_files
from .config import Config
from .errors import PubGateError
from .filtering import (
    check_conflict_markers,
    check_residual_markers,
    is_ignored,
    scrub_internal_blocks,
)
from .git import GitRepo, is_lfs_pointer
from .models import CommitInfo, format_commit

logger = logging.getLogger(__name__)


def build_stage_snapshot(
    git: GitRepo,
    ref: str,
    ignore_patterns: list[str],
    excluded: frozenset[str],
) -> tuple[dict[str, str | bytes], int]:
    tree_blobs = git.ls_tree_blob_ids(ref)
    snapshot: dict[str, str | bytes] = {}
    lfs_files: list[str] = []
    included: list[str] = []

    for path in tree_blobs:
        if path in excluded:
            logger.debug("Excluded: %s", path)
            continue
        if is_ignored(path, ignore_patterns):
            logger.debug("Ignored: %s", path)
            continue
        included.append(path)

    contents = git.read_blobs_auto([tree_blobs[path] for path in included])
    for path, content in zip(included, contents, strict=True):
        if content is None:
            continue
        if isinstance(content, bytes):
            snapshot[path] = content
        elif is_lfs_pointer(content):
            logger.debug("LFS pointer (skipping scrub): %s", path)
            lfs_files.append(path)
            snapshot[path] = content
        else:
            try:
                scrubbed = scrub_internal_blocks(content, path=path)
                if scrubbed != content:
                    logger.debug("Scrubbed internal sections: %s", path)
                content = scrubbed
                check_residual_markers(content, path)
                check_conflict_markers(content, path)
            except ValueError as exc:
                raise PubGateError(f"Error: {exc}") from exc
            snapshot[path] = content

    for path in lfs_files:
        logger.debug("  LFS: %s", path)
    logger.debug("Snapshot contains %d files", len(snapshot))
    return snapshot, len(lfs_files)


def apply_stage_snapshot(
    git: GitRepo,
    snapshot: dict[str, str | bytes],
    stage_state_file: str,
    stage_state_content: str,
    auxiliary_files: Sequence[AuxiliaryFile] = (),
) -> None:
    existing = git.ls_tree("HEAD")
    desired = set(snapshot) | {file.destination_path for file in auxiliary_files}
    changed: list[str] = []
    for path in existing:
        if path not in desired and path != stage_state_file:
            git.remove_file(path)
            changed.append(path)

    for path, content in sorted(snapshot.items()):
        git.write_file_auto(path, content)
        changed.append(path)

    copy_auxiliary_files(git.repo_dir, auxiliary_files)
    changed.extend(file.destination_path for file in auxiliary_files)

    git.write_file_auto(stage_state_file, stage_state_content)
    changed.append(stage_state_file)
    git.stage_paths(changed)


def snapshot_unchanged_ref(
    cfg: Config,
    git: GitRepo,
    snapshot: dict[str, str | bytes],
) -> str | None:
    if git.branch_exists(cfg.internal_stage_branch):
        compare_ref = cfg.internal_stage_branch
        logger.debug("Comparing snapshot against existing PR branch %s", compare_ref)
    elif git.remote_branch_exists("origin", cfg.internal_approved_branch):
        compare_ref = f"origin/{cfg.internal_approved_branch}"
        logger.debug("Comparing snapshot against origin/%s", cfg.internal_approved_branch)
    else:
        logger.debug("No previous snapshot to compare against")
        return "(empty)" if not snapshot else None

    previous_blobs = git.ls_tree_blob_ids(compare_ref)
    prev_files = set(previous_blobs) - cfg.state_files
    new_files = set(snapshot.keys()) - cfg.state_files
    if prev_files != new_files:
        return None

    paths = sorted(new_files)
    previous_contents = git.read_blobs_auto([previous_blobs[path] for path in paths])
    for path, old_content in zip(paths, previous_contents, strict=True):
        new_content = snapshot[path]
        if old_content != new_content:
            return None
    return compare_ref


def stage_commit_message(
    main_head: str,
    previous_stage_sha: str | None,
    commits: list[CommitInfo],
) -> str:
    subject = f"pubgate: filtered snapshot at {main_head[:7]}"
    if previous_stage_sha is None or not commits:
        return subject
    lines = [subject, ""]
    lines.append(f"Included commits ({previous_stage_sha[:7]}..{main_head[:7]}):")
    lines.extend(f"  {i}. {format_commit(c)}" for i, c in enumerate(commits, 1))
    return "\n".join(lines)


def ensure_public_branch(cfg: Config, git: GitRepo) -> None:
    if git.remote_branch_exists("origin", cfg.internal_approved_branch):
        return

    logger.info("Creating orphan branch '%s'...", cfg.internal_approved_branch)
    git.checkout_orphan(cfg.internal_approved_branch)
    try:
        git.rm_all_tracked()
        git.commit_allow_empty("pubgate: initialize public branch")
    except BaseException:
        try:
            git.checkout_safe(cfg.internal_main_branch)
            git.delete_branch_safe(cfg.internal_approved_branch)
        except Exception as cleanup_exc:
            logger.warning("Failed to clean up incomplete orphan branch: %s", cleanup_exc)
        raise
    git.checkout(cfg.internal_main_branch)
    git.push(cfg.internal_approved_branch, "origin", cfg.internal_approved_branch)
    logger.debug("Orphan branch '%s' created and pushed", cfg.internal_approved_branch)
