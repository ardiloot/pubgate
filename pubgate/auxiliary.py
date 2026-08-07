import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_FILE, AuxiliaryDir, parse_auxiliary_destinations
from .errors import PubGateError
from .filtering import matches_pattern
from .git import GitRepo
from .models import FileChange


@dataclass(frozen=True, slots=True)
class AuxiliaryFile:
    source_path: Path
    destination_path: str


def read_auxiliary_destinations(
    git: GitRepo,
    ref: str,
    *,
    fallback: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return parse_auxiliary_destinations(
        git.read_file_at_ref(ref, CONFIG_FILE),
        git.repo_dir,
        fallback=fallback,
    )


def is_auxiliary_path(path: str, destinations: tuple[str, ...]) -> bool:
    return any(path == destination or path.startswith(destination + "/") for destination in destinations)


def has_auxiliary_changes(changes: Sequence[FileChange], destinations: tuple[str, ...]) -> bool:
    return any(
        is_auxiliary_path(change.path, destinations)
        or (change.old_path is not None and is_auxiliary_path(change.old_path, destinations))
        for change in changes
    )


def build_auxiliary_files(
    mappings: list[AuxiliaryDir],
    *,
    internal_paths: set[str],
    forbidden_roots: Sequence[Path] = (),
) -> list[AuxiliaryFile]:
    files: list[AuxiliaryFile] = []
    destinations: set[str] = set()
    resolved_forbidden_roots = tuple(root.resolve() for root in forbidden_roots)

    for mapping in mappings:
        source = Path(os.path.abspath(mapping.source))
        try:
            resolved_source = source.resolve(strict=True)
        except OSError as exc:
            raise PubGateError(f"Error: auxiliary source '{source}' is not a readable directory.") from exc
        if resolved_source != source:
            raise PubGateError(f"Error: auxiliary source path must not contain symlinks: '{source}'.")
        source = resolved_source
        if not source.is_dir():
            raise PubGateError(f"Error: auxiliary source '{source}' is not a readable directory.")
        for forbidden in resolved_forbidden_roots:
            if source == forbidden or source.is_relative_to(forbidden) or forbidden.is_relative_to(source):
                raise PubGateError(f"Error: auxiliary source '{source}' overlaps '{forbidden}'.")

        destination_prefix = mapping.destination + "/"
        collision = next(
            (
                path
                for path in internal_paths
                if path == mapping.destination
                or path.startswith(destination_prefix)
                or mapping.destination.startswith(path + "/")
            ),
            None,
        )
        if collision is not None:
            raise PubGateError(
                f"Error: auxiliary destination '{mapping.destination}' collides with internal snapshot path "
                f"'{collision}'."
            )

        try:
            walker = os.walk(source, followlinks=False, onerror=_raise_walk_error)
            for root, directory_names, file_names in walker:
                root_path = Path(root)
                directory_names.sort()
                file_names.sort()

                for name in directory_names:
                    path = root_path / name
                    if path.is_symlink():
                        raise PubGateError(f"Error: auxiliary source contains symlink '{path}'.")

                for name in file_names:
                    path = root_path / name
                    if path.is_symlink():
                        raise PubGateError(f"Error: auxiliary source contains symlink '{path}'.")
                    if not path.is_file():
                        raise PubGateError(f"Error: auxiliary source path '{path}' is not a regular file.")
                    relative = path.relative_to(source).as_posix()
                    if not _selected(relative, mapping):
                        continue

                    destination = f"{mapping.destination}/{relative}"
                    if destination in destinations:
                        raise PubGateError(f"Error: auxiliary output path is selected more than once: '{destination}'.")
                    destinations.add(destination)
                    files.append(AuxiliaryFile(path, destination))
        except OSError as exc:
            raise PubGateError(f"Error: cannot read auxiliary source '{source}': {exc}") from exc

    return sorted(files, key=lambda file: file.destination_path)


def copy_auxiliary_files(target_root: Path, files: Sequence[AuxiliaryFile]) -> None:
    repository_root = target_root.resolve()
    parents = {Path(file.destination_path).parent for file in files}
    for relative in sorted(parents, key=lambda path: len(path.parts)):
        parent = target_root
        try:
            for part in relative.parts:
                parent /= part
                if parent.is_symlink():
                    raise PubGateError(f"Error: auxiliary destination path contains symlink '{parent}'.")
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.resolve().is_relative_to(repository_root):
                raise PubGateError(f"Error: auxiliary destination escapes the target worktree: '{parent}'.")
        except OSError as exc:
            raise PubGateError(f"Error: cannot prepare auxiliary destination '{parent}': {exc}") from exc

    for auxiliary in files:
        destination = target_root / auxiliary.destination_path
        try:
            if destination.is_symlink():
                destination.unlink()
            elif destination.is_dir():
                destination.rmdir()
            shutil.copyfile(auxiliary.source_path, destination)
        except OSError as exc:
            if destination.is_file() or destination.is_symlink():
                destination.unlink(missing_ok=True)
            raise PubGateError(f"Error: cannot copy auxiliary file '{auxiliary.source_path}': {exc}") from exc


def _selected(path: str, mapping: AuxiliaryDir) -> bool:
    included = not mapping.include or matches_pattern(path, mapping.include)
    return included and not matches_pattern(path, mapping.exclude)


def _raise_walk_error(exc: OSError) -> None:
    raise exc
