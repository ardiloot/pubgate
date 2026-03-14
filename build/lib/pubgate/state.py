from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .errors import PubGateError

if TYPE_CHECKING:
    from .git import GitRepo

__all__ = ["InboundStatus", "validate_state_sha", "StateRef"]


class InboundStatus(Enum):
    UP_TO_DATE = "up_to_date"
    NEEDS_BOOTSTRAP = "needs_bootstrap"
    NEEDS_ABSORB = "needs_absorb"


_VALID_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_state_sha(value: str, label: str) -> str:
    cleaned = value.strip()
    if not _VALID_SHA_RE.match(cleaned):
        display = cleaned[:20] + "..." if len(cleaned) > 20 else cleaned
        raise PubGateError(f"Error: {label} contains invalid SHA: '{display}'")
    return cleaned


@dataclass(frozen=True)
class StateRef:
    sha: str
    source: str

    @classmethod
    def read(cls, git: GitRepo, ref: str, path: str) -> StateRef | None:
        data = git.read_file_at_ref(ref, path)
        if data is None:
            return None
        source = f"{ref}:{path}"
        return cls(sha=validate_state_sha(data, source), source=source)
