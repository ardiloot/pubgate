from dataclasses import dataclass

__all__ = ["CommitInfo", "FileChange", "format_commit"]


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    subject: str
    author: str
    date: str


@dataclass(frozen=True, slots=True)
class FileChange:
    status: str
    path: str
    old_path: str | None = None

    @property
    def is_add(self) -> bool:
        return self.status == "A"

    @property
    def is_modify(self) -> bool:
        return self.status == "M"

    @property
    def is_delete(self) -> bool:
        return self.status == "D"

    @property
    def is_rename(self) -> bool:
        return self.status == "R"


def format_commit(c: CommitInfo) -> str:
    return f"{c.subject} ({c.sha[:7]}, {c.author}, {c.date})"
