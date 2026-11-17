from __future__ import annotations

import re
import hashlib
import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from uuid import uuid4
from typing import Any, Callable

from langgraph.types import Command

from flmh_agent.application.workflow import build_workflow
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentInput, AgentState, ConfirmationInput
from flmh_agent.ports.observability import TraceHandlerFactory
from flmh_agent.ports.coordination import RunCoordinatorPort
from flmh_agent.ports.workflow import BiQueryPort, PlannerPort, ProductContentPort


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RunConflictError(RuntimeError):
    pass


class RunNotFoundError(LookupError):
    pass


class AgentApplication:
    def __init__(
        self,
        *,
        settings: Settings,
        planner: PlannerPort,
        checkpointer: Any,
        tool_session_factory: Callable[[], AbstractAsyncContextManager[ProductContentPort]],
        trace_factory: TraceHandlerFactory,
        run_coordinator: RunCoordinatorPort,
        bi_query: BiQueryPort | None = None,
        bi_principal: str = "",
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.checkpointer = checkpointer
        self.tool_session_factory = tool_session_factory
        self.trace_factory = trace_factory
        self.run_coordinator = run_coordinator
        self.bi_query = bi_query
        self.bi_principal = bi_principal

    async def start(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            return await self._start_locked(run_id, request)

    async def _start_locked(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = AgentInput.model_validate(request)
        state: AgentState = {
            "user_request": payload.user_request,
            "slot_updates": payload.slot_updates,
        }
        fingerprint = _request_fingerprint(payload.model_dump(mode="json"))
        graph_config = self._config(run_id)
        graph = self._graph(_DisconnectedTools())
        previous = await graph.aget_state(graph_config)
        if previous.values:
            if previous.values.get("request_fingerprint") != fingerprint:
                raise RunConflictError("run_id already belongs to a different request")
            return _snapshot_response(run_id, previous)
        state["request_fingerprint"] = fingerprint
        async with self.tool_session_factory() as tools:
            graph = self._graph(tools)
            result = await graph.ainvoke(state, config=graph_config)
        return _response(run_id, result)

    async def start_new(self, request: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(uuid4())
        return await self.start(run_id, request)

    async def confirm(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            return await self._confirm_locked(run_id, request)

    async def _confirm_locked(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        decision = ConfirmationInput.model_validate(request)
        graph_config = self._config(run_id)
        graph = self._graph(_DisconnectedTools())
        previous = await graph.aget_state(graph_config)
        if not previous.values:
            raise RunNotFoundError("run_id does not exist")
        current = previous.values.get("confirmation", {}).get("confirmed")
        if "request_confirmation" not in previous.next:
            if current is decision.confirmed and not previous.next:
                return _response(run_id, previous.values)
            raise RunConflictError("run is not waiting for this confirmation")
        async with self.tool_session_factory() as tools:
            graph = self._graph(tools)
            result = await graph.ainvoke(
                Command(resume={"confirmed": decision.confirmed}),
                config=graph_config,
            )
        return _response(run_id, result)

    async def retry(self, run_id: str) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            graph_config = self._config(run_id)
            graph = self._graph(_DisconnectedTools())
            previous = await graph.aget_state(graph_config)
            if not previous.values:
                raise RunNotFoundError("run_id does not exist")
            if "request_confirmation" in previous.next:
                raise RunConflictError("waiting for confirmation; use the confirmation endpoint")
            if not previous.next:
                return await self._refresh_task(run_id, previous)
            async with self.tool_session_factory() as tools:
                graph = self._graph(tools)
                result = await graph.ainvoke(None, config=graph_config)
            return _response(run_id, result)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            graph = self._graph(_DisconnectedTools())
            snapshot = await graph.aget_state(self._config(run_id, trace=False))
            if not snapshot.values:
                return None
            return await self._refresh_task(run_id, snapshot)

    async def _refresh_task(self, run_id: str, snapshot: Any) -> dict[str, Any]:
        task = snapshot.values.get("content_task") or {}
        status = snapshot.values.get("task_status", {}).get("status")
        if snapshot.next or not task or status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return _snapshot_response(run_id, snapshot)
        task_id = task["taskId"]
        async with self.tool_session_factory() as tools:
            graph = self._graph(tools)
            current = dict(await tools.get_task(task_id))
            await graph.aupdate_state(self._config(run_id, trace=False), {"task_status": current}, as_node="read_task_status")
            result = await graph.ainvoke(None, self._config(run_id, trace=False))
        return _response(run_id, result)

    def _graph(self, tools: ProductContentPort):
        return build_workflow(
            planner=self.planner,
            tools=tools,
            bi_query=self.bi_query,
            bi_principal=self.bi_principal,
            checkpointer=self.checkpointer,
        )

    def _config(self, run_id: str, *, trace: bool = True) -> dict[str, Any]:
        config: dict[str, Any] = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 32,
        }
        handler = self.trace_factory.create() if trace else None
        if handler is not None:
            config["callbacks"] = [handler]
        return config

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must be 1 to 128 letters, digits, dots, underscores, colons, or hyphens")


class _DisconnectedTools:
    async def search_products(self, query: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def create_quote(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def confirm_quote(self, quote_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def submit_task(self, request: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def get_task(self, task_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")


def _response(run_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__", ())
    action = None
    if interrupts:
        first = interrupts[0]
        action = getattr(first, "value", first)
    values = {key: value for key, value in result.items() if key != "__interrupt__"}
    pending_confirmation = bool(action or values.get("confirmation_request") and not values.get("confirmation"))
    plan = values.get("plan") or {}
    product_results = values.get("product_results") or []
    products = [
        product
        for item in product_results
        for product in _extract_products(item)
    ]
    return {
        "runId": run_id,
        "status": ("needs_input" if values.get("required_slots") else "awaiting_confirmation" if pending_confirmation
                   else "processing" if values.get("content_task") and values.get("task_status", {}).get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED"}
                   else "completed"),
        "plan": plan,
        "slotValues": values.get("slot_values", {}),
        "slotVersion": values.get("slot_version", 0),
        "slotSnapshot": values.get("slot_snapshot", ""),
        "invalidated": values.get("invalidated", []),
        "products": products,
        "contentBrief": values.get("content_brief", {}),
        "quote": values.get("quote", {}),
        "contentTask": values.get("content_task", {}),
        "taskStatus": values.get("task_status", {}),
        "confirmationRequest": action or values.get("confirmation_request"),
        "summary": values.get("summary", ""),
        "error": values.get("error", ""),
        "biResult": values.get("bi_result", {}),
        "requiredSlots": values.get("required_slots", []),
    }


def _extract_products(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = result.get("products", result.get("data", []))
    if isinstance(value, dict):
        value = value.get("products", [])
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_response(run_id: str, snapshot: Any) -> dict[str, Any]:
    response = _response(run_id, snapshot.values)
    if snapshot.next:
        interrupted = "request_confirmation" in snapshot.next
        response["status"] = "awaiting_confirmation" if interrupted else "retryable"
        response["confirmationRequest"] = snapshot.values.get("confirmation_request") if interrupted else None
    return response
