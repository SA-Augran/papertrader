"""Optional stdio MCP tools. All account operations use the existing HTTP owner."""
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def request(path, data=None):
    base = os.getenv("PAPERTRADER_URL", "http://127.0.0.1:8322").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.path:
        raise ValueError("MCP adapter connects to a local HTTP account service")
    token = os.getenv("PAPERTRADER_TOKEN", "")
    if not token:
        raise ValueError("Set PAPERTRADER_TOKEN from the account service startup output")
    req = Request(base + path, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                  data=json.dumps(data).encode() if data is not None else None)
    try:
        with urlopen(req, timeout=45) as response:
            return json.load(response)
    except HTTPError as exc:
        raise ValueError(json.load(exc).get("error", "Account request rejected")) from None


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise SystemExit("Install the optional MCP extra with Python 3.11+: pip install '.[mcp]'")
    server = FastMCP("papertrader")

    @server.tool()
    def account_state() -> dict:
        """Read your paper account, data freshness, positions and verified learning."""
        return request("/api/state")

    @server.tool()
    def decision_context() -> dict:
        """Get today's persistent task and authorized opportunities. The daily window must be open."""
        return request("/api/context")

    @server.tool()
    def submit_decision(task_id: str, action: str, code: str, weight: float, reason: str) -> dict:
        """Submit buy/sell/hold once for a task. Repeat an identical proposal safely after disconnect."""
        return request("/api/submit", {"task_id": task_id, "proposal": {
            "action": action, "code": code, "weight": weight, "reason": reason}})

    @server.tool()
    def events_since(cursor: int = 0) -> dict:
        """Read finalized decisions and settlement feedback. Acknowledge each ID only after memory storage."""
        return request("/api/events?since=" + str(cursor))

    @server.tool()
    def record_commitment(code: str, quote: str, through: str = "") -> dict:
        """Explicitly promise to wait on a watched symbol. Dates use YYYY-MM-DD."""
        return request("/api/commitment", {"code": code, "quote": quote, "through": through or None})

    @server.tool()
    def revise_commitment(commitment_id: int, reason: str) -> dict:
        """Keep the original promise and record an explicit reason for revising it."""
        return request("/api/revise", {"id": commitment_id, "reason": reason})

    @server.tool()
    def submit_observation(text: str, source: str) -> dict:
        """Share sourced background, remembered only from the receipt time. Never overrides trading gates."""
        return request("/api/observation", {"text": text, "source": source})

    @server.tool()
    def recheck_decision(decision_id: int, ignore_commitment: bool = False) -> dict:
        """Recheck a recorded gate snapshot. This does not simulate hypothetical profit."""
        return request("/api/recheck", {"decision_id": decision_id, "ignore_commitment": ignore_commitment})

    return server


def run():
    create_server().run(transport="stdio")
