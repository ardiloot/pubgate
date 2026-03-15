import logging

from .config import Config
from .git import GitRepo
from .models import CommitInfo, format_commit

logger = logging.getLogger(__name__)


def resolve_publish_base(
    cfg: Config,
    git: GitRepo,
    absorbed_sha: str,
    public_head: str,
    preview_ref: str,
    *,
    remote_sha: str | None,
) -> tuple[str, str]:
    # Find the preview commit that was last published
    publish_log_base = absorbed_sha
    if remote_sha is not None:
        found = git.find_commit_introducing(
            absorbed_sha,
            preview_ref,
            cfg.stage_state_file,
            remote_sha,
        )
        if found:
            publish_log_base = found

    # Try to advance the base to public-remote/main HEAD by checking
    # whether public-remote/main has any non-state-file differences from
    # the last-published preview tree.
    publish_base = absorbed_sha
    if public_head != absorbed_sha:
        state_files = cfg.state_files
        changes = git.diff_tree(publish_log_base, public_head)
        external_changes = [c for c in changes if c.path not in state_files]
        if not external_changes:
            logger.debug(
                "Advancing publish base %s → %s (no external changes)",
                absorbed_sha[:7],
                public_head[:7],
            )
            publish_base = public_head
        else:
            logger.debug(
                "Keeping publish base at %s (external changes: %s)",
                absorbed_sha[:7],
                ", ".join(c.path for c in external_changes),
            )

    return publish_base, publish_log_base


def publish_commit_message(
    main_sha: str,
    preview_commits: list[CommitInfo],
    publish_log_base: str,
    preview_ref: str,
) -> str:
    subject = f"pubgate: publish stage from {main_sha[:7]}"
    lines = [subject]
    if preview_commits:
        lines.append("")
        lines.append(f"Included commits ({publish_log_base[:7]}..{preview_ref}):")
        lines.extend(f"  {i}. {format_commit(c)}" for i, c in enumerate(preview_commits, 1))
    return "\n".join(lines)
