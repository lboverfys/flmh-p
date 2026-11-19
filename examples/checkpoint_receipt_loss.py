"""从项目根运行。本进程只在指定的新报价由 Java 成功建单后中断一次。"""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.config import Settings
from main import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quote-id", required=True)
    parser.add_argument("--receipt", required=True, type=Path, help="新的证据文件路径，保存报价号、任务号和重放标记")
    args = parser.parse_args()
    os.environ.setdefault("FLMH_ENV_FILE", str(Path(__file__).resolve().parents[1] / ".env"))
    settings = Settings.from_env()
    local = {"127.0.0.1", "::1", "localhost"}
    mcp = urlsplit(settings.mcp_tools_url)
    if (settings.environment == "production" or settings.checkpointer_backend != "postgres"
            or settings.agent_host not in local or settings.mcp_host not in local
            or mcp.scheme != "http" or mcp.hostname not in local or (mcp.port or 80) != settings.mcp_port
            or {mcp.hostname, settings.mcp_host} == {"127.0.0.1", "::1"}
            or mcp.path != "/mcp" or urlsplit(settings.gateway_url).hostname not in local):
        parser.error("验收需使用本机 Java 网关和 PostgreSQL，MCP_TOOLS_URL 须指向本进程的 /mcp")
    from psycopg.conninfo import conninfo_to_dict

    pg = conninfo_to_dict(settings.postgres_checkpointer_uri)
    hosts = [pg.get(name) or os.getenv("PG" + name.upper(), "") for name in ("host", "hostaddr")]
    if (pg.get("service") or os.getenv("PGSERVICE")
            or any(host and host.lower() not in local for value in hosts for host in value.split(","))):
        parser.error("请直接指定本机 PostgreSQL 检查点地址")
    original = JavaGatewayClient.submit_task
    with args.receipt.open("x", encoding="utf-8") as evidence:
        async def drop_receipt(client, request, idempotency_key):
            answer = await original(client, request, idempotency_key)
            if request["quote_id"] == args.quote_id and idempotency_key == args.quote_id:
                if (answer.get("quoteId") != args.quote_id or not answer.get("taskId")
                        or answer.get("idempotentReplay") is not False):
                    raise RuntimeError("需以从未建单的目标报价进行验收")
                json.dump({key: answer[key] for key in ("quoteId", "taskId", "idempotentReplay")}, evidence)
                evidence.flush()
                os.fsync(evidence.fileno())
                os._exit(86)  # Java 事务已完成，MCP 结果尚未返回；仅结束本次启动的进程。
            return answer

        with patch.object(JavaGatewayClient, "submit_task", drop_receipt):
            run(verify_only=False)
    raise SystemExit("实验在目标报价首次建单回执写入前已结束")


if __name__ == "__main__":
    main()
