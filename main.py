"""Run the Agent API and MCP bridge together in this Python process."""

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import uvicorn

from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.bootstrap import runtime_loop_factory
from flmh_agent.config import ConfigurationError, Settings


def _serve(name: str, server: uvicorn.Server, settings: Settings, stop: Event) -> None:
    try:
        asyncio.run(server.serve(), loop_factory=runtime_loop_factory(settings))
        if not stop.is_set():
            raise RuntimeError(f"{name} 意外退出")
    except SystemExit as exc:  # Uvicorn reports bind and lifespan failures via SystemExit.
        raise RuntimeError(f"{name} 启动失败：{server.config.host}:{server.config.port}，详见上方日志") from exc
    finally:
        stop.set()


def _read_stop(stop: Event) -> None:
    for line in sys.stdin:
        if line.strip().lower() == "stop":
            stop.set()
            return


def run(verify_only: bool) -> None:
    os.environ.setdefault("FLMH_ENV_FILE", str(Path(__file__).with_name(".env").resolve()))
    from flmh_agent.api import app  # The standalone ASGI app reads settings on import.

    settings = app.state.settings
    mcp_app = create_mcp_server(settings).streamable_http_app()
    services = {
        "Agent API": uvicorn.Server(uvicorn.Config(app, host=settings.agent_host, port=settings.agent_port,
                                                   lifespan="on", proxy_headers=False)),
        "MCP": uvicorn.Server(uvicorn.Config(mcp_app, host=settings.mcp_host, port=settings.mcp_port,
                                              lifespan="on", proxy_headers=False)),
    }
    stop = Event()
    with ThreadPoolExecutor(max_workers=len(services), thread_name_prefix="flmh") as workers:
        try:
            tasks = [workers.submit(_serve, name, server, settings, stop) for name, server in services.items()]
            while not all(server.started for server in services.values()):
                if stop.wait(0.05):
                    break
            else:
                for name, server in services.items():
                    host = server.config.host
                    address = f"[{host}]" if ":" in host else host
                    print(f"{name} 已就绪：http://{address}:{server.config.port}{'/mcp' if name == 'MCP' else '/'}", flush=True)
                if not verify_only:
                    print("输入 stop 并回车，或按 Ctrl+C，停止两个 Python 服务。", flush=True)
                    if sys.stdin is not None:
                        Thread(target=_read_stop, args=(stop,), daemon=True).start()
                    while not stop.wait(0.25):  # Allow Ctrl+C on the Windows main thread.
                        pass
        finally:
            stop.set()
            for server in services.values():
                server.should_exit = True
        for task in tasks:
            task.result()


def main() -> int:
    parser = argparse.ArgumentParser(description="同时启动 Python Agent API 和 MCP 工具服务。")
    parser.add_argument("--verify-only", action="store_true", help="检查两个 Python 服务均已监听，然后退出")
    try:
        run(parser.parse_args().verify_only)
    except KeyboardInterrupt:
        return 0
    except (ConfigurationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
