import logging
from collections.abc import Sequence
from email import policy
from email.parser import HeaderParser

from .config import Config
from .errors import PubGateError
from .git import GitRepo

logger = logging.getLogger(__name__)


def _parse_public_identity(identity: str, label: str) -> tuple[str, str]:
    error = f"Error: {label} must be a single 'Name <email>' identity."
    if not identity.strip() or any(ord(char) < 32 or char == "\x7f" for char in identity):
        raise PubGateError(error)
    try:
        header = HeaderParser(policy=policy.default).parsestr(f"From: {identity.strip()}")["From"]
    except ValueError as exc:
        raise PubGateError(error) from exc
    if (
        header is None
        or header.defects
        or len(header.addresses) != 1
        or any(group.display_name is not None for group in header.groups)
    ):
        raise PubGateError(error)

    address = header.addresses[0]
    name = address.display_name.strip()
    if (
        not name
        or not address.username
        or not address.domain
        or any(ord(char) < 32 or char in "\x7f<>" for char in name)
    ):
        raise PubGateError(error)
    email = address.addr_spec
    if any(char in email for char in '"<>'):
        raise PubGateError(f"Error: {label} email must be Git-compatible (no quotes or angle brackets).")
    return name, email


def normalize_publish_metadata(author: str, message: str) -> tuple[str, str, str]:
    message = message.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not message:
        raise PubGateError("Error: publish message must not be empty.")
    if "\x00" in message:
        raise PubGateError("Error: publish message must not contain NUL characters.")

    author_name, author_email = _parse_public_identity(author, "public author")
    return author_name, author_email, message


def append_co_authors(git: GitRepo, message: str, co_authors: Sequence[str]) -> str:
    identities = []
    for co_author in co_authors:
        name, email = _parse_public_identity(co_author, "public co-author")
        identities.append(f"{name} <{email}>")
    if not identities:
        return message

    existing_trailers = git.parse_trailers(message)
    existing_co_authors = set()
    for trailer in existing_trailers:
        key, _, value = trailer.partition(":")
        if key.strip().casefold() == "co-authored-by":
            existing_co_authors.add(value.strip())
            try:
                name, email = _parse_public_identity(value.strip(), "existing co-author")
                existing_co_authors.add(f"{name} <{email}>")
            except PubGateError:
                pass

    trailers = [
        f"Co-authored-by: {identity}" for identity in dict.fromkeys(identities) if identity not in existing_co_authors
    ]
    if not trailers:
        return message
    separator = "\n" if existing_trailers else "\n\n"
    return message + separator + "\n".join(trailers)


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
