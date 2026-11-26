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
