import argparse
import enum
import logging
import sys
from pathlib import Path

from ._log import setup_logging
from .config import load_config
from .errors import PubGateError

logger = logging.getLogger(__name__)


class Command(enum.Enum):
    ABSORB = "absorb"
    PREVIEW = "preview"
    STAGE = "stage"
    PUBLISH = "publish"
    STATUS = "status"


def _add_common_flags(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview planned actions (fetches remotes but skips branch creation, commits, and pushes)",
    )
    subparser.add_argument(
        "--force", action="store_true", help="Overwrite existing PR branch (use when previous PR was not merged)"
    )
    subparser.add_argument(
        "--no-pr", action="store_true", help="Skip automatic PR creation (show manual steps instead)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pubgate", description="Sync internal repo <-> public repo")
    parser.add_argument("--repo-dir", default=".", help="Path to the internal repo (default: .)")

    sub = parser.add_subparsers(dest="command")
    absorb = sub.add_parser(Command.ABSORB.value, help="Bring public repo changes into internal main via PR")
    _add_common_flags(absorb)

    preview = sub.add_parser(
        Command.PREVIEW.value,
        help="Generate an optional local-only stage preview worktree for testing",
    )
    preview.add_argument("--output", required=True, help="Path for the linked preview worktree")
    preview.add_argument("--force", action="store_true", help="Reset and reuse an existing pubgate preview worktree")

    stage = sub.add_parser(
        Command.STAGE.value,
        help="Generate stage candidate and open internal PR into pubgate/public-approved",
    )
    _add_common_flags(stage)

    publish = sub.add_parser(
        Command.PUBLISH.value,
        help="Push reviewed pubgate/public-approved content to the public repo and open PR",
    )
    _add_common_flags(publish)
    publish.add_argument(
        "--message",
        required=True,
        help="Public commit message (first line becomes the PR title)",
    )
    publish.add_argument("--author-name", required=True, help="Public Git author and committer name")
    publish.add_argument("--author-email", required=True, help="Public Git author and committer email")

    sub.add_parser(Command.STATUS.value, help="Show sync status of absorb, stage, and publish")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        cfg = load_config(args.repo_dir)
    except PubGateError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    cmd = Command(args.command)

    from .core import PubGate
    from .git import GitRepo

    try:
        git = GitRepo(Path(args.repo_dir))
        git.verify_repo()
        if cmd != Command.PREVIEW:
            git.ensure_remote(cfg.public_remote, cfg.public_url)
        pg = PubGate(cfg, git)

        if cmd == Command.STATUS:
            pg.status()
        elif cmd == Command.PREVIEW:
            pg.preview(output=args.output, force=args.force)
        else:
            flags = dict(dry_run=args.dry_run, force=args.force, no_pr=args.no_pr)
            match cmd:
                case Command.ABSORB:
                    pg.absorb(**flags)
                case Command.STAGE:
                    pg.stage(**flags)
                case Command.PUBLISH:
                    pg.publish(
                        **flags,
                        message=args.message,
                        author_name=args.author_name,
                        author_email=args.author_email,
                    )
    except PubGateError as exc:
        logger.error("Command failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
