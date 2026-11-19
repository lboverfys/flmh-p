"""脚本化网关只用于核对计量与评分协议。"""
import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from flmh_agent.config import Settings
from flmh_agent.evaluate_agent import (
    DATA_DIR, ENDPOINT, HISTORY_CHARS, MAX_TOKENS, PURPOSE, Dialogue, MeasuredGateway,
    evaluate, history_window, load_cases, measure, summarize, usage_fields,
)


def reply(content, **overrides):
    return {"content": json.dumps(content, ensure_ascii=False), "source": "OPENAI_COMPATIBLE", "provider": "unit",
            "model": "unit-model", "usage": {"inputTokens": 10, "outputTokens": 2}, **overrides}


def plan(queries):
    return {"summary": "按市场查询", "queries": queries, "create_content": False, "content_type": ""}


class AgentEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cases, self.catalog, _ = load_cases(DATA_DIR, "all", set())
        self.samples = {case.id: case for items in cases.values() for case in items}

    async def play(self, case, arm, replies):
        requests = []

        def handler(request):
            self.assertEqual((request.method, request.url.path), ("POST", ENDPOINT))
            payload = json.loads(request.content)
            self.assertEqual((payload["purpose"], payload["temperature"], payload["maxTokens"], payload["responseFormat"]),
                             (PURPOSE, 0.0, MAX_TOKENS, "json_object"))
            requests.append(payload)
            response = replies[len(requests) - 1]
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://evaluation.test") as client:
            gateway = MeasuredGateway(Settings(), client, "unit", "unit-model")
            row = await measure(case, arm, gateway, self.catalog, "message", HISTORY_CHARS)
        self.assertEqual(len(requests), len(replies))
        return row, requests

    async def test_cross_market_same_observations_but_distinct_model_and_dispatch_counts(self):
        task = self.samples["bound-category"]
        origin = {"region": "US", "keyword": "露营灯"}
        ranking = {"region": "CA", "ranking_limit": 1}
        destination = {"region": "ID", "max_price": 35}
        final = {"answer": {t.query.region: t.ids for t in task.checks}}
        dag = plan([{**origin, "query_id": "a"}, ranking,
                    {**destination, "category_from_query": "a", "depends_on": ["a"]}])
        planned, requests = await self.play(task, "planner", [reply(dag), reply(final)])
        serial, _ = await self.play(task, "react", [reply({"query": origin}), reply({"query": ranking}),
                                    reply({"query": {**destination, "category": "LT-07"}}), reply(final)])
        self.assertNotIn("us-a4", requests[0]["messages"][-1]["content"])
        for row, expected in [(planned, (2, 3, 2, 1)), (serial, (4, 3, 3, 0))]:
            self.assertTrue(row["correct"])
            self.assertEqual(tuple(row["metrics"][k] for k in ("model_calls", "tool_calls", "tool_batches", "parallel_batches")), expected)
            self.assertEqual(row["metrics"]["total_tokens"], expected[0] * 12)
        self.assertEqual([len(batch) for batch in planned["tool_trace"]], [2, 1])
        self.assertEqual([event for batch in planned["tool_trace"] for event in batch],
                         [event for batch in serial["tool_trace"] for event in batch])

    async def test_answer_alone_or_simultaneous_category_guess_has_no_dependency_evidence(self):
        task = self.samples["bound-category"]
        final = {"answer": {t.query.region: t.ids for t in task.checks}}
        guess, _ = await self.play(task, "react", [reply(final)])
        self.assertFalse(guess["correct"])
        self.assertEqual(guess["metrics"]["tool_calls"], 0)
        flat = [t.query.model_dump(exclude_unset=True) for t in task.checks]
        concurrent, _ = await self.play(task, "planner", [reply(plan(flat)), reply(final)])
        self.assertFalse(concurrent["correct"])
        self.assertEqual(concurrent["query_evidence"], {"US": True, "CA": True, "ID": False})
        flat[0]["query_id"] = "origin"
        flat[-1]["depends_on"] = ["origin"]
        unbound, _ = await self.play(task, "planner", [reply(plan(flat)), reply(final)])
        self.assertEqual(unbound["metrics"]["tool_batches"], 2)
        self.assertFalse(unbound["query_evidence"]["ID"])

    async def test_failed_binding_and_react_limit_count_only_dispatched_tools(self):
        task = self.samples["bound-category"]
        dag = plan([{"region": "US", "keyword": "没有这个商品", "query_id": "x"},
                    {"region": "ID", "depends_on": ["x"], "category_from_query": "x"}])
        row, _ = await self.play(task, "planner", [reply(dag)])
        self.assertEqual(row["status"], "failed")
        self.assertIn("CATEGORY_BINDING_SOURCE_EMPTY", row["error"])
        self.assertEqual(row["metrics"]["tool_calls"], 1)
        limited, _ = await self.play(task, "react", [reply({"query": {"region": "US"}})] * 5)
        self.assertEqual(limited["status"], "failed")
        self.assertEqual((limited["metrics"]["model_calls"], limited["metrics"]["tool_calls"]), (5, 4))

    async def test_rejected_gateway_response_or_identity_is_never_counted_as_verified_usage(self):
        for bad in [httpx.Response(401), reply({}, source="LOCAL_DETERMINISTIC"),
                    reply({}, provider="fallback"), reply({}, model="other")]:
            with self.subTest(bad=bad):
                row, _ = await self.play(self.samples["single-price"], "planner", [bad])
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["metrics"]["model_calls"], 1)
                self.assertEqual(row["metrics"]["pinned_model_responses"], 0)
                self.assertIsNone(row["metrics"]["total_tokens"])
                self.assertIn("error", row["model_trace"][0])

    async def test_usage_partials_real_zero_and_invalid_json(self):
        self.assertEqual(usage_fields(None)["usage_status"], "missing")
        for value in (True, -1, "9", 1.5):
            self.assertEqual(usage_fields({"inputTokens": value})["usage_status"], "invalid")
        task = self.samples["single-price"]
        query = task.checks[0].query.model_dump(exclude_unset=True)
        final = {"answer": {"US": task.checks[0].ids}}
        row, _ = await self.play(task, "react", [reply({"query": query}, usage={"inputTokens": 0, "outputTokens": 2}),
                                                  reply(final, usage={"inputTokens": 0})])
        self.assertTrue(row["correct"])
        self.assertEqual(row["metrics"]["input_tokens"], 0)
        self.assertIsNone(row["metrics"]["output_tokens"])
        self.assertIsNone(row["metrics"]["total_tokens"])
        self.assertEqual(row["metrics"]["usage_status"], "partial")
        malformed, _ = await self.play(task, "react", [{**reply({}), "content": "这不是 JSON"}])
        self.assertEqual(malformed["status"], "failed")
        self.assertEqual(malformed["metrics"]["total_tokens"], 12)

    def test_window_counts_current_message_and_truncates_history_from_left(self):
        messages = [f"message {i}" for i in range(26)]
        for unit, count in [("message", 11), ("turn", 24)]:
            text, info = history_window(messages, unit, 1000)
            self.assertEqual(text, "\n".join(messages[-count:]))
            self.assertEqual(info, {"dropped_messages": 26 - count, "dropped_chars": 0})
        dialogue = self.samples["character-cutoff"]
        history = [line for t in dialogue.turns[:2] for line in (t.user, t.assistant)]
        text, info = history_window(history, "message", HISTORY_CHARS)
        self.assertIn("收纳盒", text)
        self.assertEqual(info, {"dropped_messages": 0, "dropped_chars": 0})
        long_history = ["x" * 1800, "y" * 1800]
        text, info = history_window(long_history, "message", HISTORY_CHARS)
        self.assertEqual(text, ("\n".join(long_history))[-HISTORY_CHARS:])
        self.assertEqual(info, {"dropped_messages": 0, "dropped_chars": 601})
        self.assertGreater(len(self.samples["window-eviction"].turns), 13)

    async def test_slots_only_store_model_updates_and_missing_is_not_an_explicit_clear(self):
        case = Dialogue.model_validate({"id": "unit-memory", "turns": [
            {"user": "美国，商品最多40，制作预算20。", "assistant": "已记录。", "check": {"region": "US"}},
            {"user": "只改制作预算到10。", "assistant": "收到改动。", "check": {"budget_max": 10, "product_max_price": 40}},
            {"user": "撤销商品限价。", "assistant": "好。", "check": {"region": "US", "product_max_price": None}}]})
        wrong_region = {"region": "GB", "product_max_price": 40, "budget_max": 20}
        replies = [reply({"updates": wrong_region, "constraints": wrong_region}),
                   reply({"updates": {"budget_max": 10}, "constraints": {**wrong_region, "budget_max": 10}}),
                   reply({"updates": {"product_max_price": None}, "constraints": {"region": "GB"}})]
        stored, slot_requests = await self.play(case, "slots", replies)
        _, window_requests = await self.play(case, "history12", replies)
        for req_a, req_b, turn in zip(slot_requests, window_requests, case.turns):
            self.assertEqual(req_a["messages"][0], req_b["messages"][0])
            for req in (req_a, req_b):
                body = json.loads(req["messages"][-1]["content"])
                self.assertEqual(set(body), {"context", "user"})
                self.assertEqual(body["user"], turn.user)
        third = json.loads(slot_requests[2]["messages"][-1]["content"])["context"]
        self.assertEqual(third["slots"], {**wrong_region, "budget_max": 10})
        self.assertEqual(third["versions"], {"region": 1, "product_max_price": 1, "budget_max": 2})
        history = json.loads(window_requests[2]["messages"][-1]["content"])["context"]["history"]
        self.assertIn("助手：收到改动。", history)
        self.assertNotIn("GB", history)
        self.assertEqual(stored["constraints"]["matched"], 2)
        self.assertEqual(stored["constraints"]["expected"], 5)
        self.assertEqual(stored["turns"][-1]["matched_fields"], [])

    async def test_failed_turn_preserves_all_labels_and_marks_unscored_followups(self):
        case = self.samples["budget-is-not-price"]
        first = case.turns[0].check.model_dump(exclude_unset=True)
        row, _ = await self.play(case, "slots", [reply({"updates": first, "constraints": first}), httpx.Response(503)])
        self.assertEqual(row["status"], "failed")
        self.assertEqual([t["status"] for t in row["turns"]], ["completed", "failed"] + ["skipped_after_error"] * 3)
        counts = row["constraints"]
        self.assertEqual(counts["matched"], len(first))
        self.assertEqual(counts["expected"], sum(len(t.check.model_fields_set) for t in case.turns))
        self.assertEqual(counts["unscored"], counts["expected"] - len(first))
        self.assertIsNone(counts["retention_rate"])
        self.assertEqual(row["metrics"]["usage_complete_calls"], 1)
        self.assertIsNone(row["metrics"]["total_tokens"])

    async def test_summary_keeps_failed_and_missing_usage_out_of_paired_statistics(self):
        task = self.samples["single-price"]
        query = task.checks[0].query.model_dump(exclude_unset=True)
        final = {"answer": {"US": task.checks[0].ids}}
        planner, _ = await self.play(task, "planner", [reply(plan([query])), reply(final)])
        react, _ = await self.play(task, "react", [reply({"query": query}), reply(final, usage=None)])
        failed, _ = await self.play(task, "planner", [httpx.Response(429)])
        records = [planner, react, {**failed, "case_id": "failed"}, {**react, "case_id": "failed"}]
        summary = summarize("planning", records)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["paired_ids"], [task.id])
        self.assertEqual(summary["usage_paired_ids"], [])
        self.assertIsNone(summary["paired_token_reduction"]["total_tokens"])
        self.assertEqual(summary["arms"]["planner"]["failed_ids"], ["failed"])
        self.assertEqual(summary["arms"]["react"]["paired_means"]["tool_calls"], 1)
        full_react, _ = await self.play(task, "react", [reply({"query": query}, usage={"inputTokens": 30, "outputTokens": 2}), reply(final)])
        complete = summarize("planning", [planner, full_react])
        self.assertEqual(complete["usage_paired_ids"], [task.id])
        self.assertEqual(complete["paired_token_reduction"]["input_tokens"], 0.5)

    async def test_file_entry_records_incomplete_usage_and_preserves_existing_delivery(self):
        task = self.samples["single-price"]
        query = task.checks[0].query.model_dump(exclude_unset=True)
        final = {"answer": {"US": task.checks[0].ids}}
        replies = iter([reply(plan([query])), reply(final), reply({"query": query}), reply(final, usage=None)])

        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer unit-user-token")
            self.assertEqual(request.headers["X-Service-Key"], "unit-service-key")
            return httpx.Response(200, json=next(replies))

        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "AGENT_EVAL_BEARER_TOKEN": "unit-user-token", "MODEL_GATEWAY_API_KEY": "unit-service-key",
            "MODEL_GATEWAY_URL": "http://evaluation.test"}, clear=True), patch.object(httpx, "AsyncClient", client):
            path = Path(tmp) / "run.jsonl"
            args = argparse.Namespace(data_dir=DATA_DIR, suite="planning", case=[task.id], output=path,
                                      model="unit-model", provider="unit", history_unit="message", history_chars=HISTORY_CHARS)
            self.assertEqual(await evaluate(args), 1)
            saved = path.read_text(encoding="utf-8")
            records = [json.loads(line) for line in saved.splitlines()]
            self.assertEqual([r["kind"] for r in records], ["metadata", "case", "case", "summary"])
            self.assertEqual(records[-1]["suites"]["planning"]["usage_paired_ids"], [])
            self.assertNotIn("unit-user-token", saved)
            self.assertNotIn("unit-service-key", saved)
            with self.assertRaises(FileExistsError):
                await evaluate(args)
            self.assertEqual(path.read_text(encoding="utf-8"), saved)
