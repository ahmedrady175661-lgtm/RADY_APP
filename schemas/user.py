from datetime import datetime

from pydantic import BaseModel, Field


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=200)
    is_admin: bool = False
    group_id: int | None = None
    max_stored_large_rows: int = Field(ge=1, le=200_000_000)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str = ""
    is_admin: bool
    is_active: bool
    device_id: str | None
    group_id: int | None = None
    group_name: str | None = None
    max_stored_large_rows: int | None = None
    used_stored_large_rows: int = 0
    used_stored_large_bytes: int = 0
    gemini_rest_model_id: str | None = None
    gemini_live_model_id: str | None = None
    gemini_spend_limit_enabled: bool = False
    gemini_spend_limit_usd: float | None = None

    class Config:
        from_attributes = True


class AdminUserDetailOut(BaseModel):
    """Admin panel: user + current subscription window + Gemini usage in this cycle only."""

    id: int
    username: str
    display_name: str = ""
    is_admin: bool
    is_active: bool
    device_id: str | None
    group_id: int | None = None
    group_name: str | None = None
    max_stored_large_rows: int | None = None
    used_stored_large_rows: int = 0
    used_stored_large_bytes: int = 0
    subscription_cycle_started_at: datetime | None = None
    subscription_cycle_ends_at: datetime | None = None
    cycle_days_remaining: int = 0
    in_grace_period: bool = False
    subscription_early_renew_at: datetime | None = None
    gemini_cycle_cost_usd: float = 0.0
    gemini_cycle_tokens: int = 0
    gemini_cycle_events: int = 0
    # Usage after admin early-renew, billed on the next cycle (shown until cycle end passes).
    gemini_carryover_cost_usd: float = 0.0
    gemini_carryover_tokens: int = 0
    gemini_carryover_events: int = 0
    # Current cycle only, by channel (Tafrigh REST vs Live check).
    gemini_rest_cost_usd: float = 0.0
    gemini_rest_tokens: int = 0
    gemini_rest_events: int = 0
    gemini_rest_models: str = ""
    gemini_live_cost_usd: float = 0.0
    gemini_live_tokens: int = 0
    gemini_live_events: int = 0
    gemini_live_models: str = ""
    gemini_rest_model_id: str | None = None
    gemini_live_model_id: str | None = None
    gemini_spend_limit_enabled: bool = False
    gemini_spend_limit_usd: float | None = None
    gemini_spend_remaining_usd: float | None = None

    class Config:
        from_attributes = True


class UserActiveUpdate(BaseModel):
    is_active: bool


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    max_stored_large_rows: int = Field(ge=1, le=200_000_000)


class GroupOut(BaseModel):
    id: int
    name: str
    max_stored_large_rows: int | None = None
    used_stored_large_rows: int = 0

    class Config:
        from_attributes = True


class GroupMemberOut(BaseModel):
    """User row for admin group detail."""

    id: int
    username: str
    display_name: str = ""
    is_admin: bool
    is_active: bool
    device_id: str | None = None
    max_stored_large_rows: int | None = None
    used_stored_large_rows: int = 0


class AdminGroupDetailOut(BaseModel):
    """Group + members in current billing / DB context."""

    id: int
    name: str
    max_stored_large_rows: int | None = None
    used_stored_large_rows: int = 0
    member_count: int = 0
    members: list[GroupMemberOut] = Field(default_factory=list)


class UserGroupUpdate(BaseModel):
    group_id: int | None = None


class UserLargeRowsLimitUpdate(BaseModel):
    max_stored_large_rows: int = Field(ge=1, le=200_000_000)


class GroupLargeRowsLimitUpdate(BaseModel):
    max_stored_large_rows: int = Field(ge=1, le=200_000_000)


class UserGeminiPolicyUpdate(BaseModel):
    gemini_rest_model_id: str | None = Field(default=None, max_length=200)
    gemini_live_model_id: str | None = Field(default=None, max_length=200)
    gemini_spend_limit_enabled: bool = False
    gemini_spend_limit_usd: float | None = Field(default=None, ge=0)
