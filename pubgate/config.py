import logging
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-untyped]

from .errors import PubGateError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = "pubgate.toml"

DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".internal/*",
    "internal/*",
    "*-internal.*",
    "*.internal.*",
    "*_internal.*",
    "*-private.*",
    "*.private.*",
    "*_private.*",
    "*.secret",
    "*.secrets",
]

# Allowed: alphanumeric, underscore, forward slash, dot, colon, hyphen
_VALID_BRANCH_RE = re.compile(r"^[a-zA-Z0-9_/.:-]+$")

# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _config_field(kind: str, *, scope: str = "", **kwargs):
    metadata: dict[str, str] = {"kind": kind}
    if scope:
        metadata["scope"] = scope
    return field(metadata=metadata, **kwargs)


def _validate_branch_name(name: str, field_name: str) -> None:
    if not _VALID_BRANCH_RE.match(name):
        raise PubGateError(f"'{field_name}' contains invalid characters: '{name}'")


# ---------------------------------------------------------------------------
# Field metadata utilities
# ---------------------------------------------------------------------------


def _fields_by_kind(*kinds: str) -> frozenset[str]:
    return frozenset(f.name for f in fields(Config) if f.metadata.get("kind") in kinds)


def _branch_scope_groups() -> dict[str, frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for f in fields(Config):
        if f.metadata.get("kind") == "branch":
            groups.setdefault(f.metadata.get("scope", ""), set()).add(f.name)
    return {k: frozenset(v) for k, v in groups.items()}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    # Internal repo
    internal_main_branch: str = _config_field("branch", scope="internal", default="main")
    internal_approved_branch: str = _config_field("branch", scope="internal", default="pubgate/public-approved")
    internal_absorb_branch: str = _config_field("branch", scope="internal", default="pubgate/absorb")
    internal_stage_branch: str = _config_field("branch", scope="internal", default="pubgate/stage")

    # Public repo
    public_url: str | None = _config_field("str", default=None)
    public_remote: str = _config_field("str", default="public-remote")
    public_main_branch: str = _config_field("branch", scope="public", default="main")
    public_publish_branch: str = _config_field("branch", scope="public", default="pubgate/publish")

    # State tracking
    absorb_state_file: str = _config_field("state", default=".pubgate-state-absorb")
    stage_state_file: str = _config_field("state", default=".pubgate-state-stage")

    # Filtering
    ignore: list[str] = _config_field("list", default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS))

    def __post_init__(self) -> None:
        for key in _fields_by_kind("branch"):
            _validate_branch_name(getattr(self, key), key)
        for keys in _branch_scope_groups().values():
            self._check_no_duplicates(keys, "branch name")
        self._check_no_duplicates(_fields_by_kind("state"), "filename")

    def _check_no_duplicates(self, keys: frozenset[str], label: str) -> None:
        seen: dict[str, str] = {}
        for key in sorted(keys):
            val = getattr(self, key)
            if val in seen:
                raise PubGateError(f"'{key}' and '{seen[val]}' share the same {label} '{val}'")
            seen[val] = key

    @property
    def public_main_ref(self) -> str:
        return f"{self.public_remote}/{self.public_main_branch}"

    @property
    def state_files(self) -> frozenset[str]:
        return frozenset({self.absorb_state_file, self.stage_state_file})


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------


def load_config(repo_dir: str | Path = ".") -> Config:
    repo_path = Path(repo_dir)
    config_path = repo_path / CONFIG_FILE

    if not config_path.is_file():
        raise PubGateError(
            f'No {CONFIG_FILE} found at {config_path.resolve()}. Create one with at least: public_url = "..."'
        )

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    logger.debug("Loaded config from %s", config_path)

    str_keys = _fields_by_kind("str", "branch", "state")
    list_keys = _fields_by_kind("list")

    unknown = set(data) - (str_keys | list_keys)
    if unknown:
        raise PubGateError(f"Unknown keys in {CONFIG_FILE}: {', '.join(sorted(unknown))}")

    kwargs: dict[str, object] = {}
    for key, val in data.items():
        if key in str_keys:
            if not isinstance(val, str):
                raise PubGateError(f"{CONFIG_FILE}: '{key}' must be a string")
        elif key in list_keys:
            if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                raise PubGateError(f"{CONFIG_FILE}: '{key}' must be a list of strings")
        kwargs[key] = val

    return Config(**kwargs)  # type: ignore[arg-type]
