import logging
from email.headerregistry import Address

from .config import Config
from .errors import PubGateError
from .git import GitRepo

logger = logging.getLogger(__name__)


def normalize_publish_metadata(message: str, author_name: str, author_email: str) -> tuple[str, str, str]:
    message = message.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not message:
        raise PubGateError("Error: publish message must not be empty.")
    if "\x00" in message:
        raise PubGateError("Error: publish message must not contain NUL characters.")

    author_name = author_name.strip()
    if not author_name or any(char in author_name for char in "\r\n\x00<>"):
        raise PubGateError("Error: public author name must be non-empty and single-line.")

    author_email = author_email.strip()
    if not author_email or any(char in author_email for char in "\r\n\x00<>"):
        raise PubGateError("Error: invalid public author email.")
    try:
        author_email = Address(addr_spec=author_email).addr_spec
    except ValueError as exc:
        raise PubGateError("Error: invalid public author email.") from exc

    return message, author_name, author_email


def resolve_publish_base(
    cfg: Config,
    git: GitRepo,
    absorbed_sha: str,
    public_head: str,
    approved_ref: str,
    *,
    remote_sha: str | None,
) -> tuple[str, str]:
    # Find the preview commit that was last published
    publish_log_base = absorbed_sha
    if remote_sha is not None:
        found = git.find_commit_introducing(
            absorbed_sha,
            approved_ref,
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
