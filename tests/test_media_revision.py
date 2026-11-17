import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from identity_support import IdentityTestCase
from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayPlanner
from flmh_agent.adapters.outbound.sessions import InMemorySessionDirectory
from flmh_agent.application.service import AgentApplication, RunConflictError, RunNotFoundError
from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity, current_identity
from flmh_agent.domain.planning import RuleBasedPlanner


def server_input(request):
    return {remote: request[local] for local, remote in (
        ("product_id", "productId"), ("image_prompt", "imagePrompt"), ("video_prompt", "videoPrompt"),
        ("aspect_ratio", "aspectRatio"), ("duration_seconds", "durationSeconds"))}


class RevisionTools:
    def __init__(self):
        self.calls = []
        self.quotes = {}
        self.runs = {}
        self.keys = {}
        self.drop_revision_response = False

    async def search_products(self, query):
        self.calls.append(("search", dict(query)))
        backpack = query.get("keyword") == "背包"
        return {"products": [{"id": "bag-1" if backpack else "cup-1", "title": "背包" if backpack else "杯子",
                              "region": query["region"], "source": "LOCAL_FIXTURE", "sourceRef": "fixture:products"}]}

    async def create_media_quote(self, request):
        self.calls.append(("quote", dict(request)))
        quote_id = f"q-{len(self.quotes) + 1}"
        reused = []
        origin_id = request.get("origin_run_id")
        if origin_id:
            origin = self.runs[origin_id]
            unchanged = all(server_input(request)[name] == origin["input"][name]
                            for name in ("productId", "imagePrompt", "aspectRatio"))
            if unchanged and origin["nodes"][0]["status"] in {"MOCKED", "SUCCEEDED"}:
                reused = ["image"]
        quote = {"quoteId": quote_id, "amount": 0, "currency": "USD", "executionMode": "MOCK_ONLY",
                 "originRunId": origin_id, "reusedNodes": reused, "input": server_input(request)}
        self.quotes[quote_id] = {"quote": quote, "request": dict(request), "confirmed": False}
        return copy.deepcopy(quote)

    async def confirm_quote(self, quote_id):
        self.calls.append(("confirm", quote_id))
        self.quotes[quote_id]["confirmed"] = True
        return {"quoteId": quote_id, "confirmed": True}

    def _start(self, quote_id, key):
        assert self.quotes[quote_id]["confirmed"]
        if key in self.keys:
            return copy.deepcopy(self.runs[self.keys[key]])
        run_id = f"media-{len(self.runs) + 1}"
        run = {"runId": run_id, "status": "QUEUED", "executionMode": "MOCK_ONLY", "artifacts": [],
               "input": self.quotes[quote_id]["quote"]["input"],
               "nodes": [{"nodeKey": "image", "status": "PENDING", "outputUrl": None},
                         {"nodeKey": "video", "status": "PENDING", "outputUrl": None}],
               "owner": current_identity().user_id}
        self.runs[run_id] = run
        self.keys[key] = run_id
        return copy.deepcopy(run)

    async def submit_media_run(self, quote_id, key):
        self.calls.append(("submit", quote_id, key))
        return self._start(quote_id, key)

    async def revise_media_run(self, run_id, quote_id, key):
        self.calls.append(("revise", run_id, quote_id, key))
        assert self.quotes[quote_id]["quote"]["originRunId"] == run_id
        result = self._start(quote_id, key)
        if self.drop_revision_response:
            self.drop_revision_response = False
            raise TimeoutError("revision response lost after acceptance")
        return result

    async def get_media_run(self, run_id):
        self.calls.append(("get", run_id))
        if self.runs[run_id]["owner"] != current_identity().user_id:
            raise PermissionError("media run belongs to another user")
        return copy.deepcopy(self.runs[run_id])


class MediaRevisionTests(IdentityTestCase):
    def setUp(self):
        self.tools = RevisionTools()
        self.checkpointer = InMemorySaver()

        @asynccontextmanager
        async def tools():
            yield self.tools

        self.application = AgentApplication(
            settings=Settings(), planner=RuleBasedPlanner(), checkpointer=self.checkpointer,
            tool_session_factory=tools, trace_factory=SimpleNamespace(create=lambda: None),
            run_coordinator=InMemoryRunCoordinator(), session_directory=InMemorySessionDirectory(),
        )

    async def started(self, run_id="agent"):
        quote = await self.application.start(run_id, {"user_request": "找美国杯子制作图片和视频，预算10美元"})
        return await self.application.confirm(run_id, {"quote_id": quote["quote"]["quoteId"], "confirmed": True})

    async def failed(self, run_id="agent"):
        result = await self.started(run_id)
        media = self.tools.runs[result["mediaRun"]["runId"]]
        media["status"] = "FAILED"
        media["nodes"][0]["status"] = "MOCKED"
        media["nodes"][1]["status"] = "FAILED"
        return copy.deepcopy(media)

    async def test_failed_video_revision_preserves_image_and_requires_new_confirmation(self):
        origin = await self.failed()
        revised = await self.application.revise("agent", {"expected_revision": 0,
            "user_request": "预算改成20美元；视频提示词：海边展示；时长改为10秒"})
        self.assertEqual(revised["status"], "awaiting_confirmation")
        self.assertEqual(revised["plan"]["source"], "RULES")
        self.assertEqual(revised["mediaOriginRun"]["runId"], origin["runId"])
        self.assertEqual(revised["mediaRequest"]["image_prompt"], origin["input"]["imagePrompt"])
        self.assertEqual(revised["mediaRequest"]["duration_seconds"], 10)
        self.assertEqual(revised["mediaRequest"]["budget_max"], 20)
        self.assertIn("海边展示", revised["mediaRequest"]["video_prompt"])
        self.assertEqual(revised["quote"]["reusedNodes"], ["image"])
        self.assertIn("10秒", revised["confirmationRequest"]["message"])
        self.assertIn("media_request", revised["invalidated"])
        self.assertEqual(len(self.tools.runs), 1)
        with self.assertRaisesRegex(RunConflictError, "stale"):
            await self.application.confirm("agent", {"quote_id": "q-1", "confirmed": True})
        completed = await self.application.confirm("agent", {"quote_id": "q-2", "confirmed": True})
        self.assertEqual(completed["mediaRun"]["runId"], "media-2")
        self.assertEqual([call[0] for call in self.tools.calls].count("submit"), 1)
        self.assertEqual([call[0] for call in self.tools.calls].count("revise"), 1)
        await self.application.confirm("agent", {"quote_id": "q-2", "confirmed": True})
        self.assertEqual(len(self.tools.runs), 2)

    async def test_product_region_image_and_ratio_changes_do_not_reuse_image(self):
        for index, revision in enumerate((
            {"user_request": "商品改为背包"}, {"user_request": "地区改为英国"},
            {"media_updates": {"image_prompt": "白底摄影"}}, {"user_request": "比例改为1:1"},
        )):
            with self.subTest(revision=revision):
                agent = f"a-{index}"
                origin = await self.failed(agent)
                result = await self.application.revise(agent, {"expected_revision": 0, **revision})
                self.assertEqual(result["quote"]["reusedNodes"], [])
                if index < 3:
                    self.assertNotEqual(result["mediaRequest"]["image_prompt"], origin["input"]["imagePrompt"])
                if index == 0:
                    self.assertEqual(result["mediaRequest"]["product_id"], "bag-1")
                    self.assertNotIn("杯子", result["mediaRequest"]["image_prompt"])
                if index == 1:
                    self.assertIn("目标地区：GB", result["mediaRequest"]["image_prompt"])

    async def test_pending_revisions_keep_origin_and_prior_edits_with_explicit_precedence(self):
        origin = await self.failed()
        first = await self.application.revise("agent", {"expected_revision": 0,
            "user_request": "视频提示词：自然语言要求；时长改为8秒",
            "media_updates": {"video_prompt": "显式字段优先", "duration_seconds": 10}})
        second = await self.application.revise("agent", {"expected_revision": 1, "slot_updates": {"budget_max": 30}})
        self.assertEqual(second["mediaOriginRun"]["runId"], origin["runId"])
        self.assertEqual(second["mediaRequest"]["duration_seconds"], 10)
        self.assertEqual(second["mediaRequest"]["video_prompt"], first["mediaRequest"]["video_prompt"])
        self.assertIn("显式字段优先", second["mediaRequest"]["video_prompt"])
        self.assertEqual(second["quote"]["reusedNodes"], ["image"])
        self.assertEqual(second["mediaRequest"]["budget_max"], 30)
        with self.assertRaisesRegex(RunConflictError, "stale"):
            await self.application.confirm("agent", {"quote_id": first["quote"]["quoteId"], "confirmed": True})
        declined = await self.application.confirm("agent", {"quote_id": second["quote"]["quoteId"], "confirmed": False})
        self.assertIn("拒绝", declined["summary"])
        self.assertEqual(len(self.tools.runs), 1)

    async def test_unknown_or_nonfailed_runs_never_get_revision_quote(self):
        for index, (run_status, node_status) in enumerate((
            ("SUBMISSION_UNKNOWN", "SUBMISSION_UNKNOWN"), ("FAILED", "SUBMISSION_UNKNOWN"),
            ("QUEUED", "PENDING"), ("RUNNING", "PENDING"), ("MOCKED", "MOCKED"),
            ("CANCELLED", "FAILED"), ("SUCCEEDED", "SUCCEEDED"),
        )):
            with self.subTest(status=run_status, node=node_status):
                agent = f"status-{index}"
                result = await self.started(agent)
                media = self.tools.runs[result["mediaRun"]["runId"]]
                media["status"] = run_status
                media["nodes"][1]["status"] = node_status
                before = len(self.tools.quotes)
                with self.assertRaises(RunConflictError):
                    await self.application.revise(agent, {"expected_revision": 0, "media_updates": {"duration_seconds": 8}})
                self.assertEqual(len(self.tools.quotes), before)
        self.assertFalse(any(call[0] == "revise" for call in self.tools.calls))

    async def test_other_owner_cannot_reference_session_or_forged_origin_field(self):
        await self.failed()
        other = UserIdentity("other-user", "USER", "LOCAL_MOCK", "Bearer other-user")
        before = len(self.tools.calls)
        with self.assertRaises(RunNotFoundError):
            await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}}, identity=other)
        self.assertEqual(len(self.tools.calls), before)
        with self.assertRaises(ValidationError):
            await self.application.revise("agent", {"expected_revision": 0, "origin_run_id": "other-media"})

    async def test_service_refuses_origin_whose_owner_has_changed(self):
        origin = await self.failed()
        self.tools.runs[origin["runId"]]["owner"] = "other-user"
        with self.assertRaises(PermissionError):
            await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}})
        self.assertEqual(len(self.tools.quotes), 1)

    async def test_lost_revision_response_retries_identical_key_and_cannot_be_edited(self):
        await self.failed()
        await self.application.revise("agent", {"expected_revision": 0, "user_request": "重跑失败视频"})
        self.tools.drop_revision_response = True
        with self.assertRaises(TimeoutError):
            await self.application.confirm("agent", {"quote_id": "q-2", "confirmed": True})
        with self.assertRaisesRegex(RunConflictError, "cannot be rewritten"):
            await self.application.revise("agent", {"expected_revision": 1, "slot_updates": {"budget_max": 30}})
        recovered = await self.application.retry("agent")
        revisions = [call for call in self.tools.calls if call[0] == "revise"]
        self.assertEqual(revisions[0], revisions[1])
        self.assertEqual(len(self.tools.runs), 2)
        self.assertEqual(recovered["mediaRun"]["runId"], "media-2")

    async def test_arbitrary_text_and_invalid_specs_are_not_silent_prompts(self):
        await self.failed()
        for request in ({"user_request": "感觉不好，再优化一下"}, {"user_request": "时长改为30秒"},
                        {"user_request": "比例改为2:1"}, {"user_request": "预算改成20；时长改为若干秒"},
                        {"user_request": "预算改成20；比例改为其他"}, {"media_updates": {"duration_seconds": True}},
                        {"media_updates": {"image_prompt": " "}}, {"expected_revision": True}):
            with self.subTest(request=request), self.assertRaises((ValueError, ValidationError)):
                await self.application.revise("agent", {"expected_revision": 0, **request})
        self.assertEqual(len(self.tools.quotes), 1)

    async def test_missing_budget_can_be_supplied_with_media_specification(self):
        initial = await self.application.start("missing", {"user_request": "美国杯子制作视频"})
        self.assertEqual(initial["status"], "needs_input")
        result = await self.application.revise("missing", {"expected_revision": 0,
            "slot_updates": {"budget_max": 10}, "media_updates": {"duration_seconds": 8, "video_prompt": "近景展示"}})
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["mediaRequest"]["duration_seconds"], 8)
        self.assertIn("近景展示", result["mediaRequest"]["video_prompt"])
        self.assertEqual(result["mediaOriginRun"], {})
        self.assertNotIn("origin_run_id", result["mediaRequest"])

    async def test_prompt_content_is_not_reinterpreted_as_slots_or_media_fields(self):
        origin = await self.failed()
        result = await self.application.revise("agent", {"expected_revision": 0,
            "user_request": "视频提示词：商品改为背包的字幕和预算50字样仅作为画面内容"})
        self.assertEqual(result["mediaRequest"]["product_id"], origin["input"]["productId"])
        self.assertEqual(result["mediaRequest"]["budget_max"], 10)
        self.assertEqual(result["mediaRequest"]["duration_seconds"], 6)
        self.assertEqual(result["quote"]["reusedNodes"], ["image"])

    async def test_origin_is_rechecked_for_each_pending_revision(self):
        origin = await self.failed()
        await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}})
        self.tools.runs[origin["runId"]]["status"] = "SUBMISSION_UNKNOWN"
        with self.assertRaisesRegex(RunConflictError, "verify"):
            await self.application.revise("agent", {"expected_revision": 1, "media_updates": {"duration_seconds": 8}})
        self.assertEqual(len(self.tools.quotes), 2)

    async def test_verified_failure_allows_revision_but_does_not_reuse_failed_image(self):
        origin = await self.failed()
        self.tools.runs[origin["runId"]]["status"] = "SUBMISSION_UNKNOWN"
        with self.assertRaises(RunConflictError):
            await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}})
        self.tools.runs[origin["runId"]]["status"] = "FAILED"
        self.tools.runs[origin["runId"]]["nodes"][0]["status"] = "FAILED"
        result = await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}})
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["quote"]["reusedNodes"], [])

    async def test_changed_image_cannot_be_marked_reused_by_bad_quote(self):
        await self.failed()
        original = self.tools.create_media_quote

        async def bad_quote(request):
            quote = await original(request)
            quote["reusedNodes"] = ["image"]
            return quote

        self.tools.create_media_quote = bad_quote
        with self.assertRaisesRegex(ValueError, "changed image"):
            await self.application.revise("agent", {"expected_revision": 0, "media_updates": {"image_prompt": "新图片"}})
        self.assertEqual(len(self.tools.runs), 1)

    async def test_quote_must_bind_verified_origin(self):
        await self.failed()
        original = self.tools.create_media_quote

        async def bad_quote(request):
            quote = await original(request)
            quote["originRunId"] = "other-run"
            return quote

        self.tools.create_media_quote = bad_quote
        with self.assertRaisesRegex(ValueError, "origin run"):
            await self.application.revise("agent", {"expected_revision": 0, "slot_updates": {"budget_max": 20}})
        self.assertEqual(len(self.tools.runs), 1)

    async def test_initial_rule_and_model_plans_use_same_media_specification(self):
        rule = await self.application.start("rule", {"user_request": "美国杯子制作视频，预算10美元；时长8秒；比例1:1；图片提示词：白底摄影"})
        self.assertEqual(rule["mediaRequest"]["duration_seconds"], 8)
        self.assertEqual(rule["mediaRequest"]["aspect_ratio"], "1:1")
        self.assertEqual(rule["plan"]["queries"][0]["keyword"], "杯子")
        self.assertIn("白底摄影", rule["mediaRequest"]["image_prompt"])
        raw = {"summary": "模型测试计划", "queries": [{"region": "US", "keyword": "杯子"}],
               "create_content": True, "content_type": "product_media", "budget_max": 10,
               "media_options": {"duration_seconds": 8, "aspect_ratio": "1:1", "image_prompt": "白底摄影"}}
        gateway = SimpleNamespace(complete=AsyncMock(return_value={"content": json.dumps(raw, ensure_ascii=False)}))
        self.application.planner = ModelGatewayPlanner(gateway)
        model = await self.application.start("model", {"user_request": "美国杯子制作视频，预算10美元；时长8秒；比例1:1；图片提示词：白底摄影"})
        self.assertEqual(model["plan"]["source"], "MODEL_GATEWAY")
        self.assertEqual(rule["mediaRequest"]["image_prompt"], model["mediaRequest"]["image_prompt"])
        self.assertEqual(rule["mediaRequest"]["duration_seconds"], model["mediaRequest"]["duration_seconds"])
        raw["media_options"]["duration_seconds"] = 1
        gateway.complete.return_value = {"content": json.dumps(raw)}
        with self.assertRaises(ValidationError):
            await self.application.start("invalid-model", {"user_request": "美国杯子视频，预算10"})
