"""Update page contracts.

None of the requests carry data: the branch and the remote are read from the
repository, never from the browser. That is why no field here is an input.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CommitInfo(BaseModel):
    sha: str = ""
    subject: str = ""
    date: str = ""


class GitStatusOut(BaseModel):
    available: bool = True  # git itself is installed
    is_repo: bool = False
    branch: str = ""
    detached: bool = False
    head: CommitInfo | None = None
    tracked_changes: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    has_upstream: bool = False
    ahead: int = 0
    behind: int = 0
    incoming: list[CommitInfo] = Field(default_factory=list)
    recent: list[CommitInfo] = Field(default_factory=list)
    last_fetch: str = ""
    error: str = ""
    can_update: bool = False
    blocked_reason: str = ""


class SchemaStatusOut(BaseModel):
    """Three separate facts, because there are no numbered migrations here."""

    expected_version: int = 0  # what the code says
    recorded_version: int = 0  # what the last startup wrote
    file_version: int | None = None  # what the database file itself says
    checked_at: str = ""
    added_columns: list[str] = Field(default_factory=list)
    in_sync: bool = True


class RunningCodeOut(BaseModel):
    version: str = ""
    boot_commit: str = ""  # what the running process started from
    head_commit: str = ""  # what is in the folder now
    restart_pending: bool = False


class LastUpdateOut(BaseModel):
    at: str = ""
    from_sha: str = ""
    to_sha: str = ""
    ok: bool = True
    message: str = ""


class UpdateStatusOut(BaseModel):
    running: RunningCodeOut
    git: GitStatusOut
    db: SchemaStatusOut
    last_update: LastUpdateOut | None = None


class CheckResultOut(BaseModel):
    ok: bool = True
    message: str = ""
    behind: int = 0
    incoming: list[CommitInfo] = Field(default_factory=list)


class UpdateResultOut(BaseModel):
    ok: bool = True
    updated: bool = False
    message: str = ""
    before: str = ""
    after: str = ""
    commits: list[CommitInfo] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    needs_install: bool = False
    restart_required: bool = False
