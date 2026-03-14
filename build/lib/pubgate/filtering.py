import fnmatch
import re
from pathlib import PurePosixPath

__all__ = ["scrub_internal_blocks", "check_residual_markers", "is_ignored"]

# ---------------------------------------------------------------------------
# Internal block markers
# ---------------------------------------------------------------------------

_BEGIN_RE = re.compile(r"^(?:#|//|<!--)\s*BEGIN-INTERNAL\s*(?:-->)?$")
_END_RE = re.compile(r"^(?:#|//|<!--)\s*END-INTERNAL\s*(?:-->)?$")


def scrub_internal_blocks(content: str, *, path: str = "<unknown>") -> str:
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    inside = False
    begin_line: int | None = None

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()

        if _BEGIN_RE.match(stripped):
            if inside:
                raise ValueError(
                    f"{path}: nested BEGIN-INTERNAL at line {lineno} (previous BEGIN at line {begin_line})"
                )
            inside = True
            begin_line = lineno
            continue

        if _END_RE.match(stripped):
            if not inside:
                raise ValueError(f"{path}: orphan END-INTERNAL at line {lineno} without a matching BEGIN-INTERNAL")
            inside = False
            begin_line = None
            continue

        if not inside:
            result.append(line)

    if inside:
        raise ValueError(f"{path}: unclosed BEGIN-INTERNAL at line {begin_line}")

    return "".join(result)


def check_residual_markers(content: str, path: str) -> None:
    for lineno, line in enumerate(content.splitlines(), start=1):
        if "BEGIN-INTERNAL" in line or "END-INTERNAL" in line:
            raise ValueError(f"{path}: residual marker found at line {lineno} after scrubbing: {line.strip()!r}")


_CONFLICT_MARKER_RE = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.MULTILINE)


def check_conflict_markers(content: str, path: str) -> None:
    m = _CONFLICT_MARKER_RE.search(content)
    if m:
        lineno = content[: m.start()].count("\n") + 1
        raise ValueError(
            f"{path}: unresolved merge-conflict marker at line {lineno}. "
            "Resolve the conflict on the absorb PR branch before staging."
        )


def is_ignored(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if fnmatch.fnmatch(PurePosixPath(path).name, pattern):
            return True
    return False
