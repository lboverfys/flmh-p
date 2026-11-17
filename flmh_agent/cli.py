from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from flmh_agent.bootstrap import open_runtime, runtime_loop_factory
from flmh_agent.config import Settings


async def run_request(user_request: str, *, confirm: bool | None = None, run_id: str | None = None, settings: Settings | None = None) -> dict:
    settings = settings or Settings.from_env()
    async with open_runtime(settings) as runtime:
        identifier = run_id or uuid.uuid4().hex
        result = await runtime.agent.start(identifier, {"user_request": user_request})
        if result["status"] != "awaiting_confirmation":
            return result
        print(json.dumps(result["confirmationRequest"], ensure_ascii=False, indent=2))
        approved = confirm if confirm is not None else input("确认报价并创建内容任务？输入 y 确认：").strip().lower() == "y"
        return await runtime.agent.confirm(identifier, {"confirmed": approved, "quote_id": result["confirmationRequest"]["quoteId"]})


def main() -> None:
    parser = argparse.ArgumentParser(description="FLMH product research Agent")
    parser.add_argument("--request", required=True, help="product research or content request")
    parser.add_argument("--run-id", help="stable run id for checkpoint recovery")
    parser.add_argument("--confirm", action="store_true", help="explicitly approve the quote in this invocation")
    arguments = parser.parse_args()
    settings = Settings.from_env()
    result = asyncio.run(
        run_request(arguments.request, confirm=True if arguments.confirm else None, run_id=arguments.run_id, settings=settings),
        loop_factory=runtime_loop_factory(settings),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
