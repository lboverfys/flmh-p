"""Start the complete local FLMH demo from the Python project."""

import argparse
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地 FLMH 演示：Java 服务、MCP、Agent 和网页工作台。")
    parser.add_argument("--verify-only", action="store_true", help="验收完整演示链路后自动关闭服务")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent
    launcher = project.parent / "flmh-j" / "start.ps1"
    if not launcher.is_file():
        parser.exit(2, f"找不到 {launcher}，请将 flmh-p 和 flmh-j 放在同一父目录。\n")
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        parser.exit(2, "启动本机演示需要 PowerShell 7（pwsh），请检查 PATH。\n")
    if sys.version_info < (3, 12) or any(find_spec(name) is None for name in ("langgraph", "mcp", "uvicorn")):
        parser.exit(2, f"当前解释器 {sys.executable} 未满足项目依赖。请选用 README 中的项目 Python 环境。\n")

    command = [pwsh, "-NoLogo", "-NoProfile", "-File", str(launcher),
               "-PythonProject", str(project), "-PythonExecutable", sys.executable]
    if args.verify_only:
        command.append("-VerifyOnly")
    print("正在启动六个 Java 服务、MCP 和 Agent；工作台就绪后将显示地址。", flush=True)
    supervisor = subprocess.Popen(command)
    try:
        return supervisor.wait()
    except KeyboardInterrupt:
        # The foreground PowerShell process receives the console interrupt too. Let its cleanup finish.
        print("正在等待启动器收尾；如仍显示工作台提示，请输入 stop。", file=sys.stderr, flush=True)
        return supervisor.wait()


if __name__ == "__main__":
    raise SystemExit(main())
