from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, with_config

from flmh_agent.domain.media_production import MediaOptions


class ProductLookupQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str = Field(min_length=1, max_length=96)

    @field_validator("product_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if value != value.strip() or value in {".", ".."} or any(ord(char) < 32 for char in value):
            raise ValueError("product_id must be a non-blank product identifier without surrounding whitespace or control characters")
        return value


class ProductHistoryQuery(ProductLookupQuery):
    limit: int = Field(default=30, strict=True, ge=1, le=100)
    before_version: int | None = Field(default=None, strict=True, ge=1, le=9223372036854775807)


class ProductDailyQuery(ProductLookupQuery):
    source: str = Field(min_length=1, max_length=96)
    limit: int = Field(default=30, strict=True, ge=1, le=100)
    before_date: date | None = None


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(pattern=r"^[A-Z]{2}$")
    tool_kind: Literal["product_search", "product_ranking", "product_trend", "product_creators"] | None = None
    ranking_limit: int | None = Field(default=None, strict=True, ge=1, le=50)
    keyword: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    product_id: str | None = Field(default=None, min_length=1, max_length=96)
    product_id_from_query: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    trend_days: int | None = Field(default=None, strict=True, ge=1, le=90)
    creator_limit: int | None = Field(default=None, strict=True, ge=1, le=50)
    query_id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    depends_on: list[str] = Field(default_factory=list, max_length=5)
    category_from_query: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

    def model_post_init(self, __context: Any) -> None:
        if self.ranking_limit is not None and any(
            x is not None for x in (self.keyword, self.category, self.max_price, self.category_from_query, self.product_id, self.product_id_from_query)
        ):
            raise ValueError("the ranking index accepts region and limit, not catalog filters")
        if self.category_from_query is not None:
            if self.category_from_query not in self.depends_on:
                raise ValueError("category_from_query must reference a direct depends_on query")
            if self.category is not None:
                raise ValueError("category_from_query cannot be combined with a static category")
        if self.product_id_from_query is not None:
            if self.product_id_from_query not in self.depends_on:
                raise ValueError("product_id_from_query must reference a direct depends_on query")
            if self.product_id is not None:
                raise ValueError("product_id_from_query cannot be combined with a static product_id")
        if self.tool_kind == "product_trend" and self.ranking_limit is not None:
            raise ValueError("product_trend cannot be combined with ranking_limit")
        if self.tool_kind == "product_creators" and self.ranking_limit is not None:
            raise ValueError("product_creators cannot be combined with ranking_limit")

    @field_validator("max_price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("max_price must be a numeric amount")
        return value


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=500)
    queries: list[ProductQuery] = Field(max_length=6)
    create_content: StrictBool
    content_type: Literal["", "product_script", "product_media"]
    target_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    budget_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    media_options: MediaOptions = Field(default_factory=MediaOptions)
    analysis_question: str | None = Field(default=None, max_length=4000)
    knowledge_query: str | None = Field(default=None, min_length=1, max_length=4000)
    product_history_id: str | None = Field(default=None, min_length=1, max_length=96)
    product_detail_id: str | None = Field(default=None, min_length=1, max_length=96)
    product_daily_id: str | None = Field(default=None, min_length=1, max_length=96)
    source: Literal["RULES", "MODEL_GATEWAY"]

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.media_options.model_dump(exclude_none=True) and self.content_type != "product_media":
            raise ValueError("media options require a product_media plan")
        if self.product_daily_id is not None:
            ProductLookupQuery(product_id=self.product_daily_id)
            if self.queries or self.analysis_question or self.knowledge_query or self.create_content or self.product_detail_id or self.product_history_id:
                raise ValueError("product daily statistics must be an independent read-only plan")
        if self.product_history_id is not None:
            ProductHistoryQuery(product_id=self.product_history_id)
            if self.queries or self.analysis_question or self.knowledge_query or self.create_content or self.product_detail_id:
                raise ValueError("product history must be an independent read-only plan")
        if self.product_detail_id is not None:
            ProductLookupQuery(product_id=self.product_detail_id)
            if self.queries or self.analysis_question or self.knowledge_query or self.create_content or self.product_history_id:
                raise ValueError("product detail must be an independent read-only plan")
        if not (self.analysis_question or self.queries or self.knowledge_query or self.product_history_id or self.product_detail_id or self.product_daily_id):
            raise ValueError("a plan must contain a product, history, detail, BI, or knowledge query")
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

    content_type: Literal["product_script", "product_media"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    target_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    source: Literal["INPUT_REQUEST"]
    provider: str


SlotAmount = Annotated[int, Field(strict=True, ge=0)] | Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


@with_config(ConfigDict(extra="forbid"))
class SlotUpdates(TypedDict, total=False):
    product: str | None
    region: str | None
    product_max_price: SlotAmount | None
    target_language: str | None
    budget_max: SlotAmount | None


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(min_length=1, max_length=4000)
    slot_updates: SlotUpdates = Field(default_factory=dict)

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
    expected_revision: int = Field(strict=True, ge=0)
    user_request: str | None = Field(default=None, min_length=1, max_length=4000)
    slot_updates: SlotUpdates = Field(default_factory=dict)
    media_updates: MediaOptions = Field(default_factory=MediaOptions)

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
    execution_batches: list[list[str]]
    product_results: list[dict[str, Any]]
    product_trends: dict[str, Any]
    product_creators: dict[str, Any]
    product_history: dict[str, Any]
    product_detail: dict[str, Any]
    product_daily: dict[str, Any]
    selected_product: dict[str, Any]
    content_brief: dict[str, Any]
    quote: dict[str, Any]
    budget_ok: bool
    confirmation: dict[str, Any]
    confirmed_quote: dict[str, Any]
    content_task: dict[str, Any]
    task_status: dict[str, Any]
    media_request: dict[str, Any]
    media_run: dict[str, Any]
    media_status: dict[str, Any]
    media_origin_run: dict[str, Any]
    media_revision_context: dict[str, Any]
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
    budget_currency_override: str | None
    last_user_request: str
    query_cache: dict[str, dict[str, Any]]
    product_results_snapshot: str
    conclusion_dependencies: dict[str, dict[str, int]]
    reused_queries: list[str]
    query_failures: dict[str, str]
    knowledge_result: dict[str, Any]
    configuration_todos: list[str]
    content_input_snapshot: str


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    queries: list[ProductQuery] = Field(max_length=6)
    create_content: StrictBool
    content_type: Literal["", "product_script", "product_media"]
    target_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    budget_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    media_options: MediaOptions = Field(default_factory=MediaOptions)
    analysis_question: str | None = Field(default=None, max_length=4000)
    knowledge_query: str | None = Field(default=None, min_length=1, max_length=4000)
    product_history_id: str | None = Field(default=None, min_length=1, max_length=96)
    product_detail_id: str | None = Field(default=None, min_length=1, max_length=96)
    product_daily_id: str | None = Field(default=None, min_length=1, max_length=96)

    @field_validator("budget_max", mode="before")
    @classmethod
    def validate_budget(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("budget_max must be a numeric amount")
        return value
