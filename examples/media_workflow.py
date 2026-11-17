"""Verify the supplied image/video state workflow through MCP; no paid media call or asset is produced."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.application.media_workflow import build_media_workflow
from flmh_agent.config import Settings


async def expect_tool_error(operation, expected_text: str) -> None:
    try:
        await operation()
    except RuntimeError as failure:
        if expected_text not in str(failure):
            raise
    else:
        raise AssertionError(f"expected tool error containing {expected_text}")


async def main() -> None:
    request = {"product_id": "camping-lamp-001", "prompt": "露营灯商品图片与短视频状态模拟", "mock_execution_confirmed": True}
    key = "media-" + uuid4().hex
    async with asyncio.timeout(30):
        async with McpProductContentAdapter(Settings()) as tools:
            await expect_tool_error(lambda: tools.start_media_workflow({**request, "mock_execution_confirmed": False}, key),
                                    "explicitly true")
            completed = await build_media_workflow(tools).ainvoke({"request": {**request, "idempotency_key": key}})
            created = completed["created"]
            replay = await tools.start_media_workflow(request, key)
            if not replay["idempotentReplay"] or replay["runId"] != created["runId"]:
                raise AssertionError("repeated media request did not replay the same persisted run")
            await expect_tool_error(lambda: tools.start_media_workflow({**request, "prompt": "different media request"}, key), "409")
            cancelled_run = await tools.start_media_workflow(request, key + "-cancel")
            cancelled = await tools.cancel_media_workflow(cancelled_run["runId"])
            if cancelled["status"] != "CANCELLED" or any(node["status"] != "CANCELLED" for node in cancelled["nodes"]):
                raise AssertionError("queued media run or node was not cancelled")
            await expect_tool_error(lambda: tools.simulate_media_workflow(cancelled_run["runId"]), "409")
            final = await tools.get_media_workflow(created["runId"])
            if final != completed["result"]:
                raise AssertionError("persisted media run changed unexpectedly after the simulation")

    print(json.dumps({
        "planSource": "SUPPLIED_EXAMPLE", "executionMode": "MOCK_ONLY", "confirmationRejected": True,
        "idempotentReplay": replay["idempotentReplay"], "conflictingInputRejected": True,
        "cancelledRunRejected": True, "result": final, "summary": completed["summary"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
