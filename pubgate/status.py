import logging

from .config import Config
from .errors import GitError, PubGateError
from .git import GitRepo
from .models import CommitInfo, format_commit
from .state import StateRef

logger = logging.getLogger(__name__)


def _log_commits(commits: list[CommitInfo], *, limit: int = 10) -> None:
    for i, c in enumerate(commits[:limit], 1):
        logger.info("  %d. %s", i, format_commit(c))
    if len(commits) > limit:
        logger.info("  ... and %d more", len(commits) - limit)


def _worktree_status(cfg: Config, git: GitRepo) -> None:
    parts: list[str] = []
    has_issues = False

    result = git._run("status", "--porcelain")
    if result.stdout.strip():
        parts.append("dirty (uncommitted changes)")
        has_issues = True
    else:
        parts.append("clean")

    current = git.current_branch()
    on_main = current == cfg.internal_main_branch
    if current == "HEAD":
        parts.append(f"detached HEAD (expected {cfg.internal_main_branch})")
        has_issues = True
    elif on_main:
        parts.append(f"on {cfg.internal_main_branch}")
    else:
        parts.append(f"on {current} (expected {cfg.internal_main_branch})")
        has_issues = True

    git.fetch("origin")

    if on_main:
        local_sha = git.rev_parse(cfg.internal_main_branch)
        remote_sha = git.rev_parse(f"origin/{cfg.internal_main_branch}")
        if local_sha == remote_sha:
            parts.append("synced with origin")
        else:
            has_issues = True
            ahead = len(git.rev_list(remote_sha, cfg.internal_main_branch))
            behind = len(git.rev_list(local_sha, f"origin/{cfg.internal_main_branch}"))
            if ahead and behind:
                parts.append(f"diverged ({ahead} ahead, {behind} behind)")
            elif ahead:
                parts.append(f"{ahead} unpushed commit(s)")
            else:
                parts.append(f"{behind} commit(s) behind origin")

    line = "Worktree: %s"
    args = ", ".join(parts)
    if has_issues:
        logger.warning(line, args)
    else:
        logger.info(line, args)


def _absorb_status(cfg: Config, git: GitRepo, public_fetch_ok: bool) -> None:
    if not public_fetch_ok:
        logger.info("Absorb (public → internal): unknown (failed to fetch %s)", cfg.public_remote)
        return

    if git.remote_branch_exists("origin", cfg.internal_absorb_branch):
        logger.info("Absorb (public → internal): PR pending")
        logger.info("  Branch '%s' → merge into %s", cfg.internal_absorb_branch, cfg.internal_main_branch)
        return

    if not git.remote_branch_exists(cfg.public_remote, cfg.public_main_branch):
        logger.info("Absorb (public → internal): error")
        logger.info("  Public repo has no '%s' branch (push at least one commit)", cfg.public_main_branch)
        return

    public_head = git.rev_parse(cfg.public_main_ref)
    origin_main = f"origin/{cfg.internal_main_branch}"
    absorb_ref = StateRef.read(git, origin_main, cfg.absorb_state_file)
    last_absorbed = absorb_ref.sha if absorb_ref else None

    if last_absorbed is None:
        logger.info("Absorb (public → internal): not initialized")
        logger.info("  → run 'pubgate absorb' to bootstrap")
    elif last_absorbed == public_head:
        logger.info("Absorb (public → internal): up to date")
        logger.info("  Public: %s  Absorbed: %s", public_head[:7], last_absorbed[:7])
    else:
        commits = git.log_oneline(last_absorbed, public_head)
        n = len(commits)
        logger.info("Absorb (public → internal): %d new %s", n, "commit" if n == 1 else "commits")
        logger.info("  Public: %s  Absorbed: %s", public_head[:7], last_absorbed[:7])
        _log_commits(commits)
        logger.info("  → run 'pubgate absorb'")


def _stage_status(cfg: Config, git: GitRepo) -> None:
    origin_main = f"origin/{cfg.internal_main_branch}"
    absorb_ref = StateRef.read(git, origin_main, cfg.absorb_state_file)

    if absorb_ref is None:
        logger.info("Stage (internal → review): blocked")
        logger.info("  → run 'pubgate absorb' first")
        return

    if git.remote_branch_exists("origin", cfg.internal_stage_branch):
        logger.info("Stage (internal → review): PR pending")
        logger.info("  Branch '%s' → merge into %s", cfg.internal_stage_branch, cfg.internal_approved_branch)
        return

    origin_approved = f"origin/{cfg.internal_approved_branch}"
    stage_ref = StateRef.read(git, origin_approved, cfg.stage_state_file)
    main_head = git.rev_parse(origin_main)

    if stage_ref is None:
        logger.info("Stage (internal → review): not yet staged")
        logger.info("  → run 'pubgate stage'")
    elif stage_ref.sha == main_head:
        logger.info("Stage (internal → review): up to date")
        logger.info("  Staged: %s → %s", stage_ref.sha[:7], cfg.internal_approved_branch)
    else:
        try:
            commits = git.log_oneline(stage_ref.sha, main_head)
        except PubGateError:
            commits = []
        n = len(commits)
        if n:
            logger.info("Stage (internal → review): %d %s since last stage", n, "commit" if n == 1 else "commits")
            logger.info("  Last staged: %s  Current: %s", stage_ref.sha[:7], main_head[:7])
            _log_commits(commits)
        else:
            logger.info("Stage (internal → review): has changes")
        logger.info("  → run 'pubgate stage'")


def _publish_status(cfg: Config, git: GitRepo, public_fetch_ok: bool) -> None:
    if not public_fetch_ok:
        logger.info("Publish (review → public): unknown (failed to fetch %s)", cfg.public_remote)
        return

    if git.remote_branch_exists(cfg.public_remote, cfg.public_publish_branch):
        logger.info("Publish (review → public): PR pending")
        logger.info("  Branch '%s' → merge into public %s", cfg.public_publish_branch, cfg.public_main_branch)
        return

    origin_approved = f"origin/{cfg.internal_approved_branch}"
    public_main = cfg.public_main_ref

    stage_ref = StateRef.read(git, origin_approved, cfg.stage_state_file)
    if stage_ref is None:
        logger.info("Publish (review → public): blocked")
        logger.info("  → run 'pubgate stage' and merge the PR first")
        return

    absorb_on_approved = StateRef.read(git, origin_approved, cfg.absorb_state_file)
    if absorb_on_approved is None:
        logger.info("Publish (review → public): blocked")
        logger.info("  → run 'pubgate absorb' and 'pubgate stage' first")
        return

    if not git.is_ancestor(absorb_on_approved.sha, public_main):
        logger.info("Publish (review → public): error")
        logger.info(
            "  Absorbed commit %s is not an ancestor of %s/main",
            absorb_on_approved.sha[:7],
            cfg.public_remote,
        )
        logger.info("  → run 'pubgate absorb' to re-sync")
        return

    remote_stage_ref = StateRef.read(git, public_main, cfg.stage_state_file)
    if remote_stage_ref is not None and remote_stage_ref.sha == stage_ref.sha:
        logger.info("Publish (review → public): up to date")
    else:
        published = remote_stage_ref.sha[:7] if remote_stage_ref else "(none)"
        logger.info("Publish (review → public): ready")
        logger.info("  Staged: %s  Last published: %s", stage_ref.sha[:7], published)
        logger.info("  → run 'pubgate publish'")


def report_status(cfg: Config, git: GitRepo) -> None:
    _worktree_status(cfg, git)

    public_fetch_ok = True
    try:
        git.fetch(cfg.public_remote)
    except (GitError, PubGateError):
        public_fetch_ok = False
        logger.info("Failed to fetch %s", cfg.public_remote)

    _absorb_status(cfg, git, public_fetch_ok)
    _stage_status(cfg, git)
    _publish_status(cfg, git, public_fetch_ok)
