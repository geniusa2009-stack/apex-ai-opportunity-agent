from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GuildCreateRequest(BaseModel):
    """POST /guilds request body -- baseline, disclosed necessary
    plumbing (see app/models/guilds.py's `Guild` docstring)."""

    name: str = Field(min_length=1, max_length=255)
    topic: str = Field(min_length=1, max_length=255)


class GuildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    topic: str
    created_by: UUID
    created_at: datetime


class PostCreateRequest(BaseModel):
    """POST /guilds/posts request body. `guild_id` is required here
    (rather than a URL path parameter) since the ask's router spec fixes
    this route's path as exactly `/guilds/posts` with no `{guild_id}`
    segment."""

    guild_id: UUID
    content: str = Field(min_length=1, max_length=5000)
    is_anonymous: bool = True


class PostOut(BaseModel):
    """`user_id` is intentionally `None` here when `is_anonymous=True` --
    the whole point of this feature is that other students see the
    `anonymous_alias`, not who really posted. The database always keeps
    the real `user_id` (see app/models/guilds.py's `GuildPost`
    docstring); this is purely an outward-facing masking choice applied
    when building this schema (see the router's `_to_post_out` helper),
    not something `from_attributes` can do on its own."""

    id: UUID
    guild_id: UUID
    user_id: UUID | None
    content: str
    anonymous_alias: str | None
    is_anonymous: bool
    created_at: datetime


class CommentCreateRequest(BaseModel):
    """POST /guilds/posts/{post_id}/comments request body."""

    content: str = Field(min_length=1, max_length=2000)
    target_text_snippet: str | None = Field(default=None, max_length=2000)
    suggested_rewrite: str | None = Field(default=None, max_length=2000)


class CommentSubmitOut(BaseModel):
    """POST /guilds/posts/{post_id}/comments response."""

    comment_id: UUID
    post_id: UUID
    is_constructive: bool | None
    rewrite_feedback: str | None
    tokens_earned: int
    tokens_balance: int


class QuestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    guild_id: UUID
    title: str
    description: str
    token_reward: int
    is_active: bool
    created_at: datetime
