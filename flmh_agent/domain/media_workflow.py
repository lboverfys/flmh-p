from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class MediaWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    prompt: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=145)
    mock_execution_confirmed: StrictBool

    @field_validator("prompt", "idempotency_key")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("media prompt and idempotency key must be non-blank without surrounding whitespace")
        return value


class MediaWorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    capabilities: dict[str, Any]
    created: dict[str, Any]
    result: dict[str, Any]
    summary: str
