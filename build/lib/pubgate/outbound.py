import logging

from .errors import PubGateError
from .filtering import check_conflict_markers, check_residual_markers, is_ignored, scrub_internal_blocks
from .git import GitRepo

logger = logging.getLogger(__name__)


def build_outbound_snapshot(
    git: GitRepo,
    ref: str,
    ignore_patterns: list[str],
    excluded: frozenset[str],
) -> dict[str, str | bytes]:
    all_files = git.ls_tree(ref)
    snapshot: dict[str, str | bytes] = {}

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
        else:
            try:
                content = scrub_internal_blocks(content, path=path)
                check_residual_markers(content, path)
                check_conflict_markers(content, path)
            except ValueError as exc:
                raise PubGateError(f"Error: {exc}") from exc
            snapshot[path] = content

    logger.debug("Snapshot contains %d files", len(snapshot))
    return snapshot
