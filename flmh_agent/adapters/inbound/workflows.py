from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from flmh_agent.adapters.outbound.workflows import WorkflowRequestError


HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Position(Input):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class Asset(Input):
    type: Literal["image", "video"]
    url: str | None = Field(default=None, min_length=1, max_length=4000)
    assetId: str | None = Field(default=None, pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

    @model_validator(mode="after")
    def one_reference(self):
        if (self.url is None) == (self.assetId is None):
            raise ValueError("asset_requires_one_reference")
        return self


class DefinitionNode(Input):
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str | None = Field(default=None, max_length=100)
    capability: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=2000)
    params: dict[str, Any] = Field(default_factory=dict, max_length=32)
    assets: list[Asset] = Field(default_factory=list, max_length=18)
    position: Position | None = None


class Edge(Input):
    parent: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    child: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    slot: str | None = Field(default=None, max_length=64)
    order: StrictInt | None = Field(default=None, ge=0, le=2147483647)


class Definition(Input):
    nodes: list[DefinitionNode] = Field(min_length=1, max_length=20)
    edges: list[Edge] = Field(default_factory=list, max_length=400)


class CreateDefinition(Input):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    definition: Definition


class UpdateDefinition(CreateDefinition):
    expectedVersion: StrictInt = Field(ge=1, le=2147483646)


class PageInput(Input):
    page: int = Field(default=1, ge=1, le=2147483647)
    pageSize: int = Field(default=20, ge=1, le=50)


class QuoteInput(Input):
    definitionVersion: StrictInt = Field(ge=1, le=2147483647)
    productId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    budgetMax: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    originRunId: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class StartInput(Input):
    quoteId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class ConfirmInput(Input):
    confirmed: StrictBool


class NodeSupport(BaseModel):
    nodeKey: str
    realAdapterSupported: bool
    reasons: list[str]


class Executability(BaseModel):
    demoMode: str
    demoSupported: bool
    demoProducesAssets: bool
    realAdapterSupported: bool
    nodes: list[NodeSupport] = Field(max_length=20)
    configurationTodos: list[str]


class DefinitionSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    version: int
    createdAt: str
    updatedAt: str


class DefinitionView(DefinitionSummary):
    definition: Definition
    definitionFingerprint: str
    executability: Executability


class DefinitionPage(BaseModel):
    workflows: list[DefinitionSummary] = Field(max_length=50)
    page: int
    pageSize: int
    total: int
    totalPages: int


class Snapshot(BaseModel):
    workflowId: str
    name: str
    definitionVersion: int
    definitionFingerprint: str
    definition: Definition
    productId: str
    executionMode: str
    configurationFingerprint: str
    nodeFingerprints: dict[str, str]
    nodePrices: dict[str, Decimal]
    reusedNodes: list[str]
    originRunId: str | None = None


class QuoteView(BaseModel):
    quoteId: str
    status: str
    amount: Decimal
    currency: str
    budgetMax: Decimal
    pricingSource: str
    snapshot: Snapshot
    createdAt: str


class RunSummary(BaseModel):
    runId: str
    quoteId: str
    status: str
    cancelRequested: bool
    createdAt: str
    updatedAt: str


class RuntimeNode(BaseModel):
    nodeKey: str
    status: str
    operation: str | None = None
    attempt: int
    requestId: str
    providerTaskId: str | None = None
    outputUrl: str | None = None
    source: str | None = None
    errorCode: str | None = None
    inputFingerprint: str
    reused: bool


class Artifact(BaseModel):
    nodeKey: str
    type: str
    url: str
    source: str


class RunView(RunSummary):
    snapshot: Snapshot
    nodes: list[RuntimeNode] = Field(max_length=20)
    artifacts: list[Artifact] = Field(max_length=20)
    idempotentReplay: bool


class RunPage(BaseModel):
    runs: list[RunSummary] = Field(max_length=50)
    total: int
    page: int
    pageSize: int
    totalPages: int


class Archived(BaseModel):
    archived: Literal[True]


class ConfirmationView(BaseModel):
    quoteId: str
    status: str


class InputLimit(BaseModel):
    min: int
    max: int


class Capability(BaseModel):
    id: str
    label: str
    output: str
    imageInput: InputLimit
    videoInput: InputLimit
    minimumTotalInputs: int
    imageSlots: list[str]
    legacyEnabled: bool
    realAdapter: str


class Capabilities(BaseModel):
    capabilities: list[Capability] = Field(max_length=100)
    maxNodes: int
    maxEdges: int
    demoExecutionMode: str
    demoProducesAssets: bool
    realExecutionRequiresConfiguration: bool


def workflow_routes(read_json) -> list[Route]:
    def handler(target, inputs, outputs, *, key_required=False):
        async def endpoint(request: Request):
            for value in request.path_params.values():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", str(value)):
                    return JSONResponse({"error": "invalid_workflow_id"}, status_code=400, headers=HEADERS)
            try:
                input_model = inputs.get(request.method)
                params = payload = key = None
                if input_model is PageInput:
                    params = input_model.model_validate(dict(request.query_params)).model_dump()
                else:
                    if request.query_params:
                        raise ValueError("unexpected_workflow_query")
                    if input_model is not None:
                        payload = input_model.model_validate(await read_json(request)).model_dump(mode="json", exclude_none=True)
                    elif request.method == "POST":
                        async for chunk in request.stream():
                            if chunk:
                                raise ValueError("workflow_action_does_not_accept_body")
                if key_required and request.method == "POST":
                    keys = request.headers.getlist("idempotency-key")
                    if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,139}", keys[0]):
                        raise ValueError("invalid_workflow_key")
                    key = keys[0]
            except ValidationError:
                return JSONResponse({"error": "invalid_workflow_input"}, status_code=422, headers=HEADERS)
            except ValueError as error:
                return JSONResponse({"error": "invalid_workflow_input"}, status_code=413 if "exceeds" in str(error) else 400, headers=HEADERS)
            try:
                result = await request.app.state.workflows.request(request.method, target.format(**request.path_params),
                        request.state.identity, params=params, payload=payload, key=key)
                safe = outputs[request.method].model_validate(result).model_dump(mode="json")
            except WorkflowRequestError as error:
                return JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)
            except ValidationError:
                return JSONResponse({"error": "invalid_workflow_response"}, status_code=502, headers=HEADERS)
            return JSONResponse(safe, headers=HEADERS)
        return endpoint

    base = "/api/v1/content/workflows"
    mappings = [
        ("/v1/workflows/meta/capabilities", base + "/meta/capabilities", {"GET": None}, {"GET": Capabilities}, False),
        ("/v1/workflows", base, {"GET": PageInput, "POST": CreateDefinition}, {"GET": DefinitionPage, "POST": DefinitionView}, False),
        ("/v1/workflows/{id:str}", base + "/{id}", {"GET": None, "PUT": UpdateDefinition, "DELETE": None},
         {"GET": DefinitionView, "PUT": DefinitionView, "DELETE": Archived}, False),
        ("/v1/workflows/{id:str}/versions/{version:int}", base + "/{id}/versions/{version}", {"GET": None}, {"GET": DefinitionView}, False),
        ("/v1/workflows/{id:str}/quotes", base + "/{id}/quotes", {"POST": QuoteInput}, {"POST": QuoteView}, False),
        ("/v1/workflows/{id:str}/runs", base + "/{id}/runs", {"POST": StartInput, "GET": PageInput}, {"POST": RunView, "GET": RunPage}, True),
        ("/v1/workflow-quotes/{id:str}/confirmation", "/api/v1/content/quotes/{id}/confirm", {"POST": ConfirmInput}, {"POST": ConfirmationView}, False),
        ("/v1/workflow-runs/{id:str}", "/api/v1/content/workflow-runs/{id}", {"GET": None}, {"GET": RunView}, False),
    ]
    for action in ("advance", "verify", "cancel"):
        mappings.append((f"/v1/workflow-runs/{{id:str}}/{action}", f"/api/v1/content/workflow-runs/{{id}}/{action}",
                         {"POST": None}, {"POST": RunView}, False))
    return [Route(path, handler(target, inputs, outputs, key_required=key), methods=list(inputs))
            for path, target, inputs, outputs, key in mappings]
