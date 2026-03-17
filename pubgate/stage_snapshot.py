import logging

from .config import Config
from .errors import PubGateError
from .filtering import check_conflict_markers, check_residual_markers, is_ignored, scrub_internal_blocks
from .git import GitRepo, is_lfs_pointer
from .models import format_commit
from .state import StateRef

logger = logging.getLogger(__name__)


def build_stage_snapshot(
    git: GitRepo,
    ref: str,
    ignore_patterns: list[str],
    excluded: frozenset[str],
) -> dict[str, str | bytes]:
    all_files = git.ls_tree(ref)
    snapshot: dict[str, str | bytes] = {}
    lfs_files: list[str] = []

    for path in all_files:
        if path in excluded:
            logger.debug("Excluded: %s", path)
            continue
        if is_ignored(path, ignore_patterns):
            logger.debug("Ignored: %s", path)
            continue

        content = git.read_file_auto(ref, path)
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
                content = scrub_internal_blocks(content, path=path)
                check_residual_markers(content, path)
                check_conflict_markers(content, path)
            except ValueError as exc:
                raise PubGateError(f"Error: {exc}") from exc
            snapshot[path] = content

    if lfs_files:
        logger.info("Snapshot includes %d LFS-tracked %s", len(lfs_files), "file" if len(lfs_files) == 1 else "files")
        for path in lfs_files:
            logger.debug("  LFS: %s", path)
    logger.debug("Snapshot contains %d files", len(snapshot))
    return snapshot


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

    prev_files = set(git.ls_tree(compare_ref)) - cfg.state_files
    new_files = set(snapshot.keys()) - cfg.state_files
    if prev_files != new_files:
        return None

    for path in new_files:
        new_content = snapshot[path]
        old_content = git.read_file_auto(compare_ref, path)
        if old_content != new_content:
            return None
    return compare_ref


def stage_commit_message(
    git: GitRepo,
    cfg: Config,
    main_head: str,
    origin_preview_ref: str,
) -> str:
    subject = f"pubgate: stage from main {main_head[:7]}"
    try:
        prev_ref = StateRef.read(git, origin_preview_ref, cfg.stage_state_file)
    except PubGateError:
        return subject
    if prev_ref is None:
        return subject
    commits = git.log_oneline(prev_ref.sha, main_head)
    if not commits:
        return subject
    lines = [subject, ""]
    lines.append(f"Included commits ({prev_ref.sha[:7]}..{main_head[:7]}):")
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
