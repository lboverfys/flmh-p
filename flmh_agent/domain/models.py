from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(pattern=r"^[A-Z]{2}$")
    keyword: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("max_price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("max_price must be a numeric amount")
        return value


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=500)
    queries: list[ProductQuery] = Field(max_length=4)
    create_content: StrictBool
    content_type: Literal["", "product_script"]
    budget_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    analysis_question: str | None = Field(default=None, max_length=4000)
    source: Literal["RULES", "MODEL_GATEWAY"]

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value

    def model_post_init(self, __context: Any) -> None:
        if bool(self.analysis_question) == bool(self.queries):
            raise ValueError("a plan must contain either product queries or one BI analysis question")
        if self.analysis_question and self.create_content:
            raise ValueError("BI analysis plans cannot create content in the same action")


class ContentBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_type: Literal["product_script"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    source: Literal["INPUT_REQUEST"]
    provider: str


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(min_length=1, max_length=4000)
    slot_updates: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_request")
    @classmethod
    def trim_request(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user_request must not be blank")
        return normalized


class AgentStartRequest(AgentInput):
    run_id: str | None = Field(default=None, max_length=128)


class TextToSqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: StrictBool


class AgentState(TypedDict, total=False):
    user_request: str
    slot_values: dict[str, Any]
    slot_updates: dict[str, Any]
    slot_version: int
    slot_snapshot: str
    input_snapshot: str
    invalidated: list[str]
    plan: dict[str, Any]
    product_results: list[dict[str, Any]]
    selected_product: dict[str, Any]
    content_brief: dict[str, Any]
    quote: dict[str, Any]
    budget_ok: bool
    confirmation: dict[str, Any]
    confirmed_quote: dict[str, Any]
    content_task: dict[str, Any]
    task_status: dict[str, Any]
    idempotency_key: str
    summary: str
    error: str
    bi_result: dict[str, Any]
    confirmation_request: dict[str, Any]
    request_fingerprint: str
    required_slots: list[str]


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    queries: list[ProductQuery] = Field(max_length=4)
    create_content: StrictBool
    content_type: Literal["", "product_script"]
    budget_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    analysis_question: str | None = Field(default=None, max_length=4000)

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value
