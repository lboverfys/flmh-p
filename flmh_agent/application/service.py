from __future__ import annotations

import asyncio
import re
import hashlib
import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4
from typing import Any, Callable

from langgraph.types import Command
from langgraph.graph import START

from flmh_agent.application.workflow import build_workflow, selected_query_region, upgrade_slot_checkpoint
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentInput, AgentState, ConfirmationInput, RevisionInput
from flmh_agent.domain.media_production import MEDIA_TERMINAL_STATUSES, MediaProductionRequest, explicit_media_options, media_status
from flmh_agent.domain.planning import explicit_revision_slots, find_budget_currency, language_revision_slots, money_revision_slots
from flmh_agent.domain.slots import normalize_slots
from flmh_agent.domain.identity import UserIdentity, bind_identity, current_identity, run_resource_key
from flmh_agent.domain.sessions import SessionCursor, SessionNotFoundError, SessionRecord, session_title
from flmh_agent.ports.observability import TraceHandlerFactory
from flmh_agent.ports.coordination import RunCoordinatorPort
from flmh_agent.ports.sessions import SessionDirectoryPort
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
        session_directory: SessionDirectoryPort,
        bi_query: BiQueryPort | None = None,
        knowledge_query: KnowledgeQueryPort | None = None,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.checkpointer = checkpointer
        self.tool_session_factory = tool_session_factory
        self.trace_factory = trace_factory
        self.run_coordinator = run_coordinator
        self.bi_query = bi_query
        self.knowledge_query = knowledge_query
        self.session_directory = session_directory

    async def start(self, run_id: str, request: Mapping[str, Any], *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity):
            return await self._start_locked(run_id, request)

    async def _start_locked(self, run_id: str, request: Mapping[str, Any], *, source: dict[str, str] | None = None) -> dict[str, Any]:
        payload = AgentInput.model_validate(request)
        normalize_slots(payload.slot_updates)
        await self.session_directory.reserve(current_identity().user_id, run_id,
                                             " ".join(payload.user_request.split())[:80])
        state: AgentState = {
            "user_request": payload.user_request,
            "slot_updates": payload.slot_updates,
            "revision": 0,
        }
        fingerprint_data = payload.model_dump(mode="json")
        if source is not None:
            state.update(source)
            fingerprint_data.update(source)
        fingerprint = _request_fingerprint(fingerprint_data)
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

    async def start_new(self, request: Mapping[str, Any], *, identity: UserIdentity | None = None) -> dict[str, Any]:
        run_id = str(uuid4())
        return await self.start(run_id, request, identity=identity)

    async def confirm(self, run_id: str, request: Mapping[str, Any], *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity):
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
                config=graph_config, **self._production_checkpoint_options(),
            )
        return _response(run_id, result)

    async def retry(self, run_id: str, *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity):
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
                write_options = self._production_checkpoint_options()
                if write_options and "plan_request" in previous.next:
                    # Planning failed or was interrupted. Its replacement plan may be read-only.
                    result = await graph.ainvoke(None, config=graph_config, interrupt_before=["create_quote"])
                    if not result["plan"]["create_content"]:
                        return _response(run_id, result)
                    previous = await graph.aget_state(graph_config)
                    if "create_quote" not in previous.next:
                        return _snapshot_response(run_id, previous)
                elif failed_queries:
                    resume_from = "retrieve_knowledge" if previous.values["plan"].get("knowledge_query") else "plan_request"
                    await graph.aupdate_state(graph_config, {"query_failures": {}, "error": ""}, as_node=resume_from)
                read_only_tail = {"read_task_status", "read_media_status", "summarize"}
                can_write = previous.values.get("plan", {}).get("create_content") and (
                    failed_queries or any(step not in read_only_tail for step in previous.next))
                options = write_options if can_write else {}
                result = await graph.ainvoke(None, config=graph_config, **options)
            return _response(run_id, result)

    async def get(self, run_id: str, *, identity: UserIdentity | None = None) -> dict[str, Any] | None:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity):
            graph = self._graph(_DisconnectedTools())
            snapshot = await graph.aget_state(self._config(run_id, trace=False))
            if not snapshot.values:
                return None
            return await self._refresh_task(run_id, snapshot)

    async def observe(self, run_id: str, *, identity: UserIdentity | None = None) -> dict[str, Any] | None:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity, synchronize=False):
            snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
            return _snapshot_response(run_id, snapshot) if snapshot.values else None

    async def list_sessions(self, *, limit: int = 20, cursor: str | None = None,
                            identity: UserIdentity | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("session page size must be between 1 and 50")
        owner = identity or current_identity()
        return (await self.session_directory.list(owner.user_id, limit, SessionCursor.decode(cursor))).view()

    async def get_session(self, run_id: str, *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity, synchronize=False) as record:
            snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
            if snapshot.values:
                record = await self._synchronize_snapshot(run_id, snapshot, record)
            if record is None:
                raise RunNotFoundError("session does not exist")
            return {"session": record.view(), "run": _snapshot_response(run_id, snapshot) if snapshot.values else None}

    async def rename_session(self, run_id: str, title: str, expected_version: int,
                             *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        _validate_session_version(expected_version)
        title = session_title(title)
        async with self._run_scope(run_id, identity, synchronize=False) as record:
            await self._ensure_session(run_id, record)
            return (await self.session_directory.rename(current_identity().user_id, run_id, title, expected_version)).view()

    async def delete_session(self, run_id: str, expected_version: int,
                             *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        _validate_session_version(expected_version)
        async with self._run_scope(run_id, identity, synchronize=False, allow_deleted=True) as record:
            await self._ensure_session(run_id, record)
            return (await self.session_directory.delete(current_identity().user_id, run_id, expected_version)).view()

    async def _ensure_session(self, run_id: str, record: SessionRecord | None) -> SessionRecord:
        if record is not None:
            return record
        snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
        if not snapshot.values:
            raise RunNotFoundError("session does not exist")
        return await self._synchronize_snapshot(run_id, snapshot, record)

    async def _synchronize_snapshot(self, run_id: str, snapshot: Any, record: SessionRecord | None) -> SessionRecord:
        owner = current_identity().user_id
        if record is None:
            title = " ".join(snapshot.values["user_request"].split())[:80]
            await self.session_directory.reserve(owner, run_id, title)
        result = _snapshot_response(run_id, snapshot)
        preview = " ".join((result["summary"] or snapshot.values["user_request"]).split())[:280]
        return await self.session_directory.synchronize(owner, run_id, preview, result["status"])

    async def _synchronize_session(self, run_id: str, record: SessionRecord | None) -> None:
        snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
        if snapshot.values:
            await self._synchronize_snapshot(run_id, snapshot, record)

    async def revise(self, run_id: str, request: Mapping[str, Any], *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        revision = RevisionInput.model_validate(request)
        normalize_slots(revision.slot_updates)
        async with self._run_scope(run_id, identity):
            graph_config = self._config(run_id)
            previous = await self._graph(_DisconnectedTools()).aget_state(graph_config)
            if not previous.values:
                raise RunNotFoundError("run_id does not exist")
            if previous.values.get("revision", 0) != revision.expected_revision:
                raise RunConflictError("revision changed; reload the current run before editing")
            values = upgrade_slot_checkpoint(previous.values)
            media = values.get("media_run")
            if values.get("content_task") or values.get("confirmed_quote") and not media:
                raise RunConflictError("confirmed production cannot be rewritten; create a new run")
            media_updates = revision.media_updates.model_dump(exclude_none=True)
            if media_updates and values.get("plan", {}).get("content_type") != "product_media":
                raise ValueError("media_updates require an existing product_media plan")
            text = revision.user_request or ""
            plan = values.get("plan", {})
            money = money_revision_slots(text, plan["create_content"]) if text and plan else {}
            lang = language_revision_slots(text) if text and plan else {}
            user_request = values["user_request"] if (money or lang) else text or values["user_request"]
            contextual_media_edit = bool(values.get("plan", {}).get("content_type") == "product_media" and (
                media or values.get("media_origin_run") or media_updates or revision.slot_updates or money
                or revision.user_request and re.search(r"改为|改成|修改为|修改成|调整为|调整成|提示词|时长|比例", revision.user_request)
            ))
            updates = {**money, **lang, **revision.slot_updates}
            overrides = {**values.get("slot_overrides", {}), **updates}
            currency = values.get("budget_currency_override") if user_request == values["user_request"] else None
            if "budget_max" in money:
                currency = find_budget_currency(text, create_content=plan["create_content"]) or currency
            if (not contextual_media_edit and user_request == values["user_request"]
                    and overrides == values.get("slot_overrides", {}) and currency == values.get("budget_currency_override")):
                return _snapshot_response(run_id, previous)
            state = {"user_request": user_request, "slot_updates": updates, "budget_currency_override": currency,
                     "revision": revision.expected_revision + 1}
            fingerprint_input = {"user_request": user_request, "slot_updates": overrides}
            if currency:
                fingerprint_input["budget_currency"] = currency
            async with self._tool_session() as tools:
                if contextual_media_edit:
                    origin = media or values.get("media_origin_run")
                    prior_context = values.get("media_revision_context", {})
                    quoted_request = values.get("media_request")
                    base_request = dict(quoted_request or prior_context.get("request", {}))
                    base_slots = prior_context.get("slots", values.get("slot_values", {}))
                    if quoted_request:
                        base_slots = {**values["slot_values"], "region": selected_query_region(values)}
                    base_request.pop("origin_run_id", None)
                    if origin:
                        origin = media_status(await tools.get_media_run(origin["runId"]), origin["runId"])
                        if origin["status"] == "SUBMISSION_UNKNOWN" or any(
                            node.get("status") == "SUBMISSION_UNKNOWN" for node in origin.get("nodes", [])
                        ):
                            raise RunConflictError("submission outcome is unknown; verify before revising")
                        if origin["status"] != "FAILED":
                            raise RunConflictError("media production cannot be rewritten; only verified FAILED runs support revision")
                        if media:
                            source = origin["input"]
                            base_request = MediaProductionRequest(
                                product_id=source["productId"], image_prompt=source["imagePrompt"],
                                video_prompt=source["videoPrompt"], aspect_ratio=source["aspectRatio"],
                                duration_seconds=source["durationSeconds"], budget_max=plan["budget_max"],
                            ).model_dump(mode="json")
                    explicit_slots = explicit_revision_slots(text)
                    explicit_media = explicit_media_options(text).model_dump(exclude_none=True)
                    if text and not (explicit_slots or explicit_media or revision.slot_updates or media_updates
                                     or re.fullmatch(r"(?:请)?(?:重跑|重试)(?:失败的?)?(?:图片|视频|媒体|任务|节点|子图|流程)?[。！!]?", text)):
                        raise ValueError("media revision requires explicit product, region, price, budget or media fields; arbitrary text is not a video prompt")
                    updates = {**explicit_slots, **revision.slot_updates}
                    media_updates = {**explicit_media, **media_updates}
                    if not quoted_request:
                        media_updates = {**plan.get("media_options", {}), **media_updates}
                    overrides = {**values.get("slot_overrides", {}), **updates}
                    state.update({
                        "slot_updates": updates,
                        "media_origin_run": origin or {},
                        "media_revision_context": {
                            "request": base_request, "slots": base_slots, "plan": plan, "updates": media_updates,
                            "budget_currency": find_budget_currency(text) or plan.get("budget_currency"),
                        },
                    })
                    fingerprint_input.update(slot_updates=overrides, media_updates=media_updates)
                else:
                    state.update({"media_revision_context": {}, "media_origin_run": {}})
                state["request_fingerprint"] = _request_fingerprint(fingerprint_input)
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

    async def advance_media(self, run_id: str, *, verify: bool = False, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        async with self._run_scope(run_id, identity):
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

    async def handoff_media(self, run_id: str, request: Mapping[str, Any] | None = None, *, identity: UserIdentity | None = None) -> dict[str, Any]:
        self._validate_run_id(run_id)
        req = dict(request or {})
        user = identity or current_identity()
        async with self._run_scope(run_id, user, synchronize=False):
            snapshot = await self._graph(_DisconnectedTools()).aget_state(self._config(run_id, trace=False))
            if not snapshot.values:
                raise RunNotFoundError("run_id does not exist")
            values = upgrade_slot_checkpoint(snapshot.values)
            task = values.get("content_task") or {}
            final = values.get("task_status") or task
            task_id = task.get("taskId")
            raw_script = final.get("generatedContent")
            if (not task_id or final.get("taskId") != task_id or final.get("status") != "SUCCEEDED"
                    or not isinstance(raw_script, str) or not raw_script.strip()):
                raise RunConflictError("media handoff requires a successful source task with final content")
            source = {"source_run_id": run_id, "source_task_id": task_id,
                      "source_content_sha256": hashlib.sha256(raw_script.encode("utf-8")).hexdigest()}
            selected = dict(values.get("selected_product") or {})
            product_id = str(req.get("product_id") or selected.get("id") or "").strip()
            if not product_id:
                raise ValueError("selected_product or product_id is required to hand off to media production")
            plan = values.get("plan") or {}
            region = (
                selected_query_region(values)
                if plan.get("queries")
                else values.get("slot_values", {}).get("region") or self.settings.agent_default_region
            )
            slots = dict(values.get("slot_values") or {})
            budget = req.get("budget_max")
            if budget is None:
                budget = slots.get("budget_max")
            if budget is None:
                budget = plan.get("budget_max")
            if budget is None:
                budget = 20
            budget_num = float(budget)
            if budget_num < 0.01:
                raise ValueError("budget_max must be at least 0.01")
            aspect_ratio = str(req.get("aspect_ratio") or "9:16")
            if aspect_ratio not in {"1:1", "3:4", "4:3", "9:16", "16:9"}:
                raise ValueError("aspect_ratio must be one of 1:1, 3:4, 4:3, 9:16, 16:9")
            duration = req.get("duration_seconds")
            if duration is None:
                duration = slots.get("duration_seconds")
            if duration is None:
                duration = (plan.get("media_options") or {}).get("duration_seconds")
            if duration is None:
                duration = plan.get("duration_seconds")
            if duration is None:
                duration = 6
            if not isinstance(duration, int) or not 2 <= duration <= 15:
                raise ValueError("duration_seconds must be between 2 and 15")
            title = str(selected.get("title") or product_id)
            compact_script = re.sub(r"[，,；;。\n]+", " ", raw_script).strip()[:220]
            clean_title = re.sub(r"[，,；;。\n]+", " ", title).strip()
            image_prompt = re.sub(
                r"[，,；;。\n]+",
                " ",
                str(req.get("image_prompt") or f"{clean_title} 商业棚拍展示图 参考要点 {compact_script}"),
            ).strip()[:600]
            video_prompt = re.sub(
                r"[，,；;。\n]+",
                " ",
                str(req.get("video_prompt") or f"围绕 {clean_title} 拍摄短视频展示画面 卖点参考 {compact_script}"),
            ).strip()[:600]
            slot_updates: dict[str, Any] = {"product": product_id, "region": region, "budget_max": budget_num}
            lang = req.get("target_language") or slots.get("target_language") or plan.get("target_language")
            if lang:
                slot_updates["target_language"] = lang
            new_run_id = str(req.get("new_run_id") or uuid4())
            media_user_request = (
                f"为{region}商品 {product_id} 制作图片和视频，"
                f"图片提示词：{image_prompt}，视频提示词：{video_prompt}，"
                f"比例：{aspect_ratio}，时长：{duration}秒，创作预算{budget_num:g}"
            )
        self._validate_run_id(new_run_id)
        async with self._run_scope(new_run_id, user):
            return await self._start_locked(new_run_id, {"user_request": media_user_request, "slot_updates": slot_updates}, source=source)

    def _graph(self, tools: ProductContentPort):
        return build_workflow(
            planner=self.planner,
            tools=tools,
            bi_query=self.bi_query,
            bi_principal=current_identity().role,
            knowledge_query=self.knowledge_query,
            checkpointer=self.checkpointer,
            settings=self.settings,
        )

    def _tool_session(self) -> _LazyToolSession:
        return _LazyToolSession(self.tool_session_factory)

    def _production_checkpoint_options(self) -> dict[str, Any]:
        return {"durability": "sync"} if self.settings.checkpointer_backend == "postgres" else {}

    @asynccontextmanager
    async def _run_scope(self, run_id: str, identity: UserIdentity | None, *, synchronize: bool = True,
                         allow_deleted: bool = False):
        user = identity or current_identity()
        with bind_identity(user):
            async with self.run_coordinator.acquire(run_resource_key(user, run_id)):
                record = await self.session_directory.find(user.user_id, run_id)
                if record is not None and record.deleted_at is not None and not allow_deleted:
                    raise RunNotFoundError("run does not exist")
                original = None
                try:
                    yield record
                except SessionNotFoundError as failure:
                    original = failure
                    raise RunNotFoundError("session does not exist") from failure
                except BaseException as failure:
                    original = failure
                    raise
                finally:
                    if synchronize:
                        await complete_cleanup(self._synchronize_session(run_id, record), original)

    def _config(self, run_id: str, *, trace: bool = True) -> dict[str, Any]:
        config: dict[str, Any] = {
            "configurable": {"thread_id": run_resource_key(current_identity(), run_id)},
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

    async def get_creator_observations(self, product_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_detail(self, product_id: str) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_daily(self, product_id: str, source: str, *, limit: int = 30, before_date: str | None = None) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_trend(self, product_id: str, *, days: int = 30) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def get_product_creators(self, product_id: str, *, limit: int = 20) -> Mapping[str, Any]:
        raise RuntimeError("state lookup does not use the product tools")

    async def search_creators(self, region: str = "US", *, keyword: str | None = None, category: str | None = None, limit: int = 20) -> Mapping[str, Any]:
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

    async def revise_media_run(self, run_id: str, quote_id: str, idempotency_key: str) -> Mapping[str, Any]:
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

    async def get_creator_observations(self, product_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).get_creator_observations(product_id)

    async def get_product_detail(self, product_id: str) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_detail(product_id)

    async def get_product_daily(self, product_id: str, source: str, *, limit: int = 30, before_date: str | None = None) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_daily(product_id, source, limit=limit, before_date=before_date)

    async def get_product_trend(self, product_id: str, *, days: int = 30) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_trend(product_id, days=days)

    async def get_product_creators(self, product_id: str, *, limit: int = 20) -> Mapping[str, Any]:
        return await (await self._connected()).get_product_creators(product_id, limit=limit)

    async def search_creators(self, region: str = "US", *, keyword: str | None = None, category: str | None = None, limit: int = 20) -> Mapping[str, Any]:
        return await (await self._connected()).search_creators(region, keyword=keyword, category=category, limit=limit)

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

    async def revise_media_run(self, run_id: str, quote_id: str, idempotency_key: str) -> Mapping[str, Any]:
        return await (await self._connected()).revise_media_run(run_id, quote_id, idempotency_key)


def _response(run_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__", ())
    action = None
    if interrupts:
        first = interrupts[0]
        action = getattr(first, "value", first)
    values = upgrade_slot_checkpoint({key: value for key, value in result.items() if key != "__interrupt__"})
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
        "executionBatches": values.get("execution_batches", []),
        "slotValues": values.get("slot_values", {}),
        "targetLanguage": values.get("content_brief", {}).get("target_language") or values.get("slot_values", {}).get("target_language"),
        "slotVersion": values.get("slot_version", 0),
        "slotVersions": values.get("slot_versions", {}),
        "revision": values.get("revision", 0),
        "conclusionDependencies": values.get("conclusion_dependencies", {}),
        "reusedQueries": values.get("reused_queries", []),
        "failedQueries": values.get("query_failures", {}),
        "slotSnapshot": values.get("slot_snapshot", ""),
        "invalidated": values.get("invalidated", []),
        "products": products,
        "productTrends": values.get("product_trends", {}),
        "productCreators": values.get("product_creators", {}),
        "productHistory": values.get("product_history", {}),
        "productDetail": values.get("product_detail", {}),
        "productDaily": values.get("product_daily", {}),
        "contentBrief": values.get("content_brief", {}),
        "quote": values.get("quote", {}),
        "contentTask": values.get("content_task", {}),
        "taskStatus": values.get("task_status", {}),
        **({"sourceRunId": values["source_run_id"], "sourceTaskId": values["source_task_id"],
            "sourceContentSha256": values["source_content_sha256"]} if values.get("source_run_id") else {}),
        "mediaRequest": values.get("media_request", {}),
        "mediaRun": values.get("media_run", {}),
        "mediaStatus": values.get("media_status", {}),
        "mediaOriginRun": values.get("media_origin_run", {}),
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


def _validate_session_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9223372036854775807:
        raise ValueError("session version must be a positive integer")


def _snapshot_response(run_id: str, snapshot: Any) -> dict[str, Any]:
    response = _response(run_id, snapshot.values)
    if snapshot.next:
        interrupted = "request_confirmation" in snapshot.next
        response["status"] = "awaiting_confirmation" if interrupted else "retryable"
        response["confirmationRequest"] = snapshot.values.get("confirmation_request") if interrupted else None
    return response
