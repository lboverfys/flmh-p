from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Any

from langgraph.types import Command

from .contracts import AgentState
from .graph import build_agent_graph


async def run_demo(user_request: str, confirmation: bool | None = None) -> dict[str, Any]:
    graph = build_agent_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state: AgentState = {"user_request": user_request}
    result = await graph.ainvoke(state, config=config)
    if "__interrupt__" not in result:
        return result

    interrupt_value = result["__interrupt__"][0].value
    print("预算确认（MOCK/Demo）：")
    print(interrupt_value)
    approved = confirmation if confirmation is not None else input("确认此报价并创建任务？输入 y 确认：").strip().lower() == "y"
    return await graph.ainvoke(Command(resume={"confirmed": approved}), config=config)


def main() -> None:
    parser = argparse.ArgumentParser(description="FLMH local Demo Agent")
    parser.add_argument("--request", required=True, help="中文选品或内容请求")
    parser.add_argument("--confirm", action="store_true", help="在命令行中显式确认报价")
    args = parser.parse_args()
    result = asyncio.run(run_demo(args.request, confirmation=True if args.confirm else None))
    print(result.get("summary", result))


if __name__ == "__main__":
    main()
