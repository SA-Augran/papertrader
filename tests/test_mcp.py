"""Optional real stdio protocol test against the local account HTTP owner."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertrader.service import AccountService, make_server


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP extra is not installed")
class MCPIntegration(unittest.TestCase):
    def test_stdio_client_uses_same_task_and_account(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as folder:
            service = AccountService(folder, sandbox=True)
            service.refresh()
            server = make_server(service, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            async def scenario():
                env = {**os.environ, "PAPERTRADER_URL": f"http://127.0.0.1:{server.server_port}",
                       "PAPERTRADER_TOKEN": service.token,
                       "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
                params = StdioServerParameters(command=sys.executable, args=["-m", "papertrader", "mcp"], env=env)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual(len(tools.tools), 8)
                        task_result = await session.call_tool("decision_context", {})
                        self.assertFalse(task_result.isError)
                        task = json.loads(task_result.content[0].text)
                        proposal = {"task_id": task["task_id"], "action": "buy",
                                    "code": task["allowed_actions"]["buy"][0], "weight": .05, "reason": "MCP trial"}
                        result = await session.call_tool("submit_decision", proposal)
                        self.assertFalse(result.isError)
                        record = json.loads(result.content[0].text)
                        self.assertEqual(record["outcome"]["status"], "filled")
                        repeated = await session.call_tool("submit_decision", proposal)
                        self.assertEqual(json.loads(repeated.content[0].text)["id"], record["id"])
                        events = await session.call_tool("events_since", {"cursor": 0})
                        self.assertEqual(json.loads(events.content[0].text)["cursor"], record["id"])
                        result = await session.call_tool("account_state", {})
                        self.assertEqual(len(json.loads(result.content[0].text)["positions"]), 1)
            try:
                asyncio.run(asyncio.wait_for(scenario(), timeout=30))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                service.close()


if __name__ == "__main__":
    unittest.main()
