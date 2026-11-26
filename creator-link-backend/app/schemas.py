"""Request models. Response bodies are assembled explicitly in main.py."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class CredentialsIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    # Optional for local demos: registration always mints a bearer session.
    password: str | None = Field(default=None, min_length=8, max_length=128)


class InvitationCreateIn(BaseModel):
    ttl_seconds: int = Field(default=24 * 3600, ge=1, le=30 * 24 * 3600)
    invitee_username: str | None = Field(default=None, max_length=32)


class ProjectCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(default="", max_length=100_000)


class CurationCreateIn(BaseModel):
    """One proposed replacement of a half-open code-point range in a revision.

    ``[start, end)`` refers to the content of ``base_revision`` (an existing
    project revision number).  An empty range is an insertion and an empty
    replacement string is a deletion.  Submitting never edits the project.
    """

    base_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    replacement: str = Field(max_length=100_000)

    @model_validator(mode="after")
    def require_ordered_range(self) -> "CurationCreateIn":
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class ProjectUpdateIn(BaseModel):
    # Unknown fields, including an attempted owner_id, are ignored. Ownership
    # is only ever read from projects.owner_id and is never updated.
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def require_a_field(self) -> "ProjectUpdateIn":
        if self.title is None and self.content is None:
            raise ValueError("at least one of title or content is required")
        return self


# ---------------------------------------------------------------------------
# Limited-edition issues
# ---------------------------------------------------------------------------


class IssueCreateIn(BaseModel):
    """Snapshot one existing revision with a fixed total quota."""

    source_revision: int = Field(ge=1)
    total_quota: int = Field(ge=1, le=1_000_000)
    # Required so a retried create never mints a second issue.
    idempotency_key: str = Field(min_length=1, max_length=200)


class ChannelAllocateIn(BaseModel):
    """Move quota from the actor's channel to a new managed child channel."""

    manager_username: str = Field(min_length=3, max_length=32)
    quota: int = Field(ge=1, le=1_000_000)
    # Omit only when the actor manages exactly one usable source channel.
    parent_channel_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


class IssueInvitationCreateIn(BaseModel):
    reader_username: str = Field(min_length=3, max_length=32)
    # Claim deadline, relative to now.  After it passes the seat is released.
    ttl_seconds: int = Field(default=7 * 24 * 3600, ge=1, le=365 * 24 * 3600)
    # Optional on the issue-level endpoint; the channel-scoped endpoint fixes it.
    channel_id: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
