from __future__ import annotations

import asyncio
import re
import hashlib
import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from uuid import uuid4
from typing import Any, Callable

from langgraph.types import Command
from langgraph.graph import START

from flmh_agent.application.workflow import build_workflow
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentInput, AgentState, ConfirmationInput, RevisionInput
from flmh_agent.domain.media_production import MEDIA_TERMINAL_STATUSES, media_status
from flmh_agent.domain.slots import normalize_slots
from flmh_agent.ports.observability import TraceHandlerFactory
from flmh_agent.ports.coordination import RunCoordinatorPort
from flmh_agent.ports.workflow import BiQueryPort, KnowledgeQueryPort, PlannerPort, ProductContentPort
from flmh_agent.resources import complete_cleanup


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
        knowledge_query: KnowledgeQueryPort | None = None,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.checkpointer = checkpointer
        self.tool_session_factory = tool_session_factory
        self.trace_factory = trace_factory
        self.run_coordinator = run_coordinator
        self.bi_query = bi_query
        self.bi_principal = bi_principal
        self.knowledge_query = knowledge_query

    async def start(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            return await self._start_locked(run_id, request)

    async def _start_locked(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = AgentInput.model_validate(request)
        state: AgentState = {
            "user_request": payload.user_request,
            "slot_updates": payload.slot_updates,
            "revision": 0,
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
        async with self._tool_session() as tools:
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
        if previous.values.get("quote", {}).get("quoteId") != decision.quote_id:
            raise RunConflictError("confirmation refers to a stale or different quote")
        current = previous.values.get("confirmation", {}).get("confirmed")
        if "request_confirmation" not in previous.next:
            if current is decision.confirmed and not previous.next:
                return _response(run_id, previous.values)
            raise RunConflictError("run is not waiting for this confirmation")
        async with self._tool_session() as tools:
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
            failed_queries = previous.values.get("query_failures")
            if not previous.next and not failed_queries:
                return await self._refresh_task(run_id, previous)
            async with self._tool_session() as tools:
                graph = self._graph(tools)
                if failed_queries:
                    resume_from = "retrieve_knowledge" if previous.values["plan"].get("knowledge_query") else "plan_request"
                    await graph.aupdate_state(graph_config, {"query_failures": {}, "error": ""}, as_node=resume_from)
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

    async def revise(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_run_id(run_id)
        revision = RevisionInput.model_validate(request)
        normalize_slots(revision.slot_updates)
        async with self.run_coordinator.acquire(run_id):
            graph_config = self._config(run_id)
            previous = await self._graph(_DisconnectedTools()).aget_state(graph_config)
            if not previous.values:
                raise RunNotFoundError("run_id does not exist")
            if previous.values.get("revision", 0) != revision.expected_revision:
                raise RunConflictError("revision changed; reload the current run before editing")
            if previous.values.get("confirmed_quote") or previous.values.get("content_task") or previous.values.get("media_run"):
                raise RunConflictError("confirmed production cannot be rewritten; create a new run")
            user_request = revision.user_request or previous.values["user_request"]
            overrides = {**previous.values.get("slot_overrides", {}), **revision.slot_updates}
            if user_request == previous.values["user_request"] and overrides == previous.values.get("slot_overrides", {}):
                return _snapshot_response(run_id, previous)
            state = {"user_request": user_request, "slot_updates": revision.slot_updates,
                     "revision": revision.expected_revision + 1,
                     "request_fingerprint": _request_fingerprint({"user_request": user_request, "slot_updates": overrides})}
            async with self._tool_session() as tools:
                graph = self._graph(tools)
                await graph.aupdate_state(graph_config, state, as_node=START)
                result = await graph.ainvoke(None, config=graph_config)
            return _response(run_id, result)

    async def _refresh_task(self, run_id: str, snapshot: Any) -> dict[str, Any]:
        media = snapshot.values.get("media_run")
        if media:
            if not snapshot.next and snapshot.values.get("media_status", {}).get("status") in MEDIA_TERMINAL_STATUSES:
                return _snapshot_response(run_id, snapshot)
            async with self._tool_session() as tools:
                current = media_status(await tools.get_media_run(media["runId"]), media["runId"])
                return await self._save_media_status(run_id, tools, current)
        task = snapshot.values.get("content_task") or {}
        status = snapshot.values.get("task_status", {}).get("status")
        if snapshot.next or not task or status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return _snapshot_response(run_id, snapshot)
        task_id = task["taskId"]
        async with self._tool_session() as tools:
            graph = self._graph(tools)
            current = dict(await tools.get_task(task_id))
            await graph.aupdate_state(self._config(run_id, trace=False), {"task_status": current}, as_node="read_task_status")
            result = await graph.ainvoke(None, self._config(run_id, trace=False))
        return _response(run_id, result)

    async def advance_media(self, run_id: str, *, verify: bool = False) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self.run_coordinator.acquire(run_id):
            snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
            if not snapshot.values:
                raise RunNotFoundError("run_id does not exist")
            media = snapshot.values.get("media_run")
            if snapshot.next or not media or not snapshot.values.get("confirmed_quote"):
                raise RunConflictError("a confirmed media run must finish its current Agent step first")
            async with self._tool_session() as tools:
                current = media_status(await tools.get_media_run(media["runId"]), media["runId"])
                if current["status"] in MEDIA_TERMINAL_STATUSES:
                    return await self._save_media_status(run_id, tools, current)
                if verify:
                    if current["status"] != "SUBMISSION_UNKNOWN":
                        raise RunConflictError("only an unknown submission can be verified")
                    result = await tools.verify_media_run(media["runId"])
                else:
                    if current["status"] == "SUBMISSION_UNKNOWN":
                        raise RunConflictError("submission outcome is unknown; verify before advancing")
                    result = await tools.advance_media_run(media["runId"])
                return await self._save_media_status(run_id, tools, media_status(result, media["runId"]))

    async def _save_media_status(self, run_id: str, tools: ProductContentPort, current: dict[str, Any]) -> dict[str, Any]:
        graph = self._graph(tools)
        config = self._config(run_id, trace=False)
        await graph.aupdate_state(config, {"media_status": current}, as_node="read_media_status")
        return _response(run_id, await graph.ainvoke(None, config))

    def _graph(self, tools: ProductContentPort):
        return build_workflow(
            planner=self.planner,
            tools=tools,
            bi_query=self.bi_query,
            bi_principal=self.bi_principal,
            knowledge_query=self.knowledge_query,
            checkpointer=self.checkpointer,
        )

    def _tool_session(self) -> _LazyToolSession:
        return _LazyToolSession(self.tool_session_factory)

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

    async def get_product_history(self, product_id: str, *, limit: int = 30, before_version: int | None = None) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_detail(self, product_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_daily(self, product_id: str, source: str, *, limit: int = 30, before_date: str | None = None) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def create_quote(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def confirm_quote(self, quote_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def submit_task(self, request: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def get_task(self, task_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the content tools")

    async def create_media_quote(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the media tools")

    async def submit_media_run(self, quote_id: str, idempotency_key: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the media tools")

    async def get_media_run(self, run_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the media tools")

    async def advance_media_run(self, run_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the media tools")

    async def verify_media_run(self, run_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the media tools")


class _LazyToolSession:
    def __init__(self, factory: Callable[[], AbstractAsyncContextManager[ProductContentPort]]) -> None:
        self._factory = factory
        self._tools: ProductContentPort | None = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._owner: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _LazyToolSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._owner is not None:
            self._stop.set()
            if not self._ready.is_set():
                self._owner.cancel()
            await complete_cleanup(self._owner, exc)

    async def _own_session(self) -> None:
        try:
            async with self._factory() as tools:
                self._tools = tools
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            if not self._stop.is_set():
                raise
        finally:
            self._tools = None
            self._ready.set()

    async def _connected(self) -> ProductContentPort:
        if self._tools is not None:
            return self._tools
        async with self._lock:
            if self._owner is None:
                self._owner = asyncio.create_task(self._own_session())
            await self._ready.wait()
            if self._tools is None:
                await self._owner
                raise RuntimeError("MCP tool session closed before becoming available")
        return self._tools

    async def search_products(self, query: Mapping[str, Any]) -> Mapping[str, Any]:
        return await (await self._connected()).search_products(query)

    async def get_product_history(self, product_id: str, *, limit: int = 30, before_version: int | None = None) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_history(product_id, limit=limit, before_version=before_version)

    async def get_product_detail(self, product_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_detail(product_id)

    async def get_product_daily(self, product_id: str, source: str, *, limit: int = 30, before_date: str | None = None) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_daily(product_id, source, limit=limit, before_date=before_date)

    async def create_quote(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return await (await self._connected()).create_quote(request)

    async def confirm_quote(self, quote_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).confirm_quote(quote_id)

    async def submit_task(self, request: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        return await (await self._connected()).submit_task(request, idempotency_key)

    async def get_task(self, task_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).get_task(task_id)

    async def create_media_quote(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return await (await self._connected()).create_media_quote(request)

    async def submit_media_run(self, quote_id: str, idempotency_key: str) -> Mapping[str, Any]:
        return await (await self._connected()).submit_media_run(quote_id, idempotency_key)

    async def get_media_run(self, run_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).get_media_run(run_id)

    async def advance_media_run(self, run_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).advance_media_run(run_id)

    async def verify_media_run(self, run_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).verify_media_run(run_id)


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
        "status": ("needs_configuration" if values.get("configuration_todos") else "retryable" if values.get("query_failures") else "needs_input" if values.get("required_slots") else "awaiting_confirmation" if pending_confirmation
                   else "needs_verification" if values.get("media_status", {}).get("status") == "SUBMISSION_UNKNOWN"
                   else "processing" if values.get("media_run") and values.get("media_status", {}).get("status") not in MEDIA_TERMINAL_STATUSES
                   else "processing" if values.get("content_task") and values.get("task_status", {}).get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED"}
                   else "completed"),
        "plan": plan,
        "slotValues": values.get("slot_values", {}),
        "slotVersion": values.get("slot_version", 0),
        "slotVersions": values.get("slot_versions", {}),
        "revision": values.get("revision", 0),
        "conclusionDependencies": values.get("conclusion_dependencies", {}),
        "reusedQueries": values.get("reused_queries", []),
        "failedQueries": values.get("query_failures", {}),
        "slotSnapshot": values.get("slot_snapshot", ""),
        "invalidated": values.get("invalidated", []),
        "products": products,
        "productHistory": values.get("product_history", {}),
        "productDetail": values.get("product_detail", {}),
        "productDaily": values.get("product_daily", {}),
        "contentBrief": values.get("content_brief", {}),
        "quote": values.get("quote", {}),
        "contentTask": values.get("content_task", {}),
        "taskStatus": values.get("task_status", {}),
        "mediaRequest": values.get("media_request", {}),
        "mediaRun": values.get("media_run", {}),
        "mediaStatus": values.get("media_status", {}),
        "confirmationRequest": action or values.get("confirmation_request"),
        "summary": values.get("summary", ""),
        "error": values.get("error", ""),
        "biResult": values.get("bi_result", {}),
        "requiredSlots": values.get("required_slots", []),
        "knowledgeResult": values.get("knowledge_result", {}),
        "configurationTodos": values.get("configuration_todos", []),
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
