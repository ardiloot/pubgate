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
    STAGE = "stage"
    PUBLISH = "publish"


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
    for cmd, help_text in (
        (Command.ABSORB.value, "Bring public repo changes into internal main via PR"),
        (Command.STAGE.value, "Generate stage candidate and open internal PR into pubgate/public-approved"),
        (Command.PUBLISH.value, "Push reviewed pubgate/public-approved content to the public repo and open PR"),
    ):
        sp = sub.add_parser(cmd, help=help_text)
        _add_common_flags(sp)

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
        git.ensure_remote(cfg.public_remote, cfg.public_url)
        pg = PubGate(cfg, git)

        flags = dict(dry_run=args.dry_run, force=args.force, no_pr=args.no_pr)
        match cmd:
            case Command.ABSORB:
                pg.absorb(**flags)
            case Command.STAGE:
                pg.stage(**flags)
            case Command.PUBLISH:
                pg.publish(**flags)
    except PubGateError as exc:
        logger.error("Command failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
