from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(pattern=r"^[A-Z]{2}$")
    keyword: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    query_id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    depends_on: list[str] = Field(default_factory=list, max_length=3)

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
    knowledge_query: str | None = Field(default=None, min_length=1, max_length=4000)
    source: Literal["RULES", "MODEL_GATEWAY"]

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value

    def model_post_init(self, __context: Any) -> None:
        if not (self.analysis_question or self.queries or self.knowledge_query):
            raise ValueError("a plan must contain a product, BI, or knowledge query")
        if self.analysis_question and (self.queries or self.knowledge_query):
            raise ValueError("BI analysis cannot share a plan with product or knowledge queries")
        if self.analysis_question and self.create_content:
            raise ValueError("BI analysis plans cannot create content in the same action")
        if self.create_content and not self.queries:
            raise ValueError("content production requires a product query")
        identifiers = [query.query_id or f"query-{index + 1}" for index, query in enumerate(self.queries)]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("query IDs must be unique")
        remaining = {identifier: set(query.depends_on) for identifier, query in zip(identifiers, self.queries)}
        if any(dependencies - set(identifiers) for dependencies in remaining.values()):
            raise ValueError("query dependencies must reference an existing query")
        completed: set[str] = set()
        while remaining:
            ready = [identifier for identifier, dependencies in remaining.items() if dependencies <= completed]
            if not ready:
                raise ValueError("query dependencies must not contain a cycle")
            completed.update(ready)
            for identifier in ready:
                del remaining[identifier]


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
    quote_id: str = Field(min_length=1, max_length=128)


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    user_request: str | None = Field(default=None, min_length=1, max_length=4000)
    slot_updates: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_request")
    @classmethod
    def trim_request(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("user_request must not be blank")
        return value.strip() if value is not None else None


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
    revision: int
    slot_versions: dict[str, int]
    slot_overrides: dict[str, Any]
    last_user_request: str
    query_cache: dict[str, dict[str, Any]]
    conclusion_dependencies: dict[str, dict[str, int]]
    reused_queries: list[str]
    query_failures: dict[str, str]
    knowledge_result: dict[str, Any]
    configuration_todos: list[str]
    content_input_snapshot: str


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    queries: list[ProductQuery] = Field(max_length=4)
    create_content: StrictBool
    content_type: Literal["", "product_script"]
    budget_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    analysis_question: str | None = Field(default=None, max_length=4000)
    knowledge_query: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value
