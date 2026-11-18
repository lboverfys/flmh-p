import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ENTRY = Path(__file__).resolve().parents[1] / "main.py"
HOST = "127.0.0.1"


class PythonEntrypointTests(unittest.TestCase):
    def reserve(self, port=0):
        listener = self.enterContext(socket.socket())
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((HOST, port))
        listener.listen()
        return listener

    def setUp(self):
        self.agent = self.reserve()
        self.mcp = self.reserve()
        self.agent_port = self.agent.getsockname()[1]
        self.mcp_port = self.mcp.getsockname()[1]

    def launch(self, *args, stdin=""):
        # Run from outside the repository, without Java, PowerShell or a developer's service settings on PATH.
        cwd = self.enterContext(tempfile.TemporaryDirectory())
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE"}}
        env.update(PATH=str(Path(sys.executable).parent), TEMP=cwd, TMP=cwd, TMPDIR=cwd, PYTHONUTF8="1",
                   FLMH_ENV_FILE=str(Path(cwd) / ".env"), AGENT_HTTP_PORT=str(self.agent_port),
                   MCP_HTTP_PORT=str(self.mcp_port), MCP_TOOLS_URL=f"http://{HOST}:{self.mcp_port}/mcp")
        return subprocess.run([sys.executable, "-B", str(ENTRY), *args], input=stdin, capture_output=True,
                              encoding="utf-8", timeout=30, env=env, cwd=cwd)

    def test_root_entry_starts_both_services_and_stop_releases_both_ports(self):
        self.agent.close()
        self.mcp.close()
        result = self.launch(stdin="stop\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"http://{HOST}:{self.agent_port}/health/ready", result.stdout)
        self.assertIn(f"http://{HOST}:{self.mcp_port}/mcp", result.stdout)
        self.reserve(self.agent_port)
        self.reserve(self.mcp_port)

    def test_occupied_mcp_port_preserves_owner_and_releases_agent_port(self):
        self.agent.close()
        result = self.launch("--verify-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{HOST}:{self.mcp_port}", result.stderr)
        with socket.create_connection((HOST, self.mcp_port), timeout=2):
            connection, _ = self.mcp.accept()
            connection.close()
        self.reserve(self.agent_port)
