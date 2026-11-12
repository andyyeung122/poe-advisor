"""MCP server (stdio) exposing the advisor's tools to Claude Code, so it runs on a Claude subscription.

Implements the small part of the Model Context Protocol a tools-only server needs
(initialize, ping, tools/list, tools/call) as newline-delimited JSON-RPC 2.0, without the
`mcp` package: one less dependency to install through a TLS-intercepting proxy.

Run by Claude Code via .mcp.json:  python -m poe_advisor.mcp_server
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO

from . import __version__
from .tools import TOOL_SCHEMAS, Toolbox

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    "Path of Exile 1 data tools: poe.ninja prices and characters, the official trade API, Path of Building import, "
    "and a local knowledge base of Reddit posts. Every price you quote must come from these tools, with its league."
)


class Server:
    def __init__(self, toolbox: Toolbox, out: BinaryIO):
        self.toolbox = toolbox
        self.out = out
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4)  # slow tools (trade, Reddit) must not block pings

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self.out.write(data)
            self.out.flush()

    def _reply(self, msg_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        self.send(message)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "poe-advisor", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, msg_id: Any, params: dict[str, Any]) -> None:
        content, is_error = self.toolbox.run(params.get("name", ""), params.get("arguments") or {})
        self._reply(msg_id, {"content": [{"type": "text", "text": content}], "isError": is_error})

    def handle(self, message: dict[str, Any]) -> None:
        method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method is None:
            return  # a response to something we never send
        if msg_id is None:
            return  # notifications (initialized, cancelled, ...) need no answer
        if method == "initialize":
            self._reply(msg_id, self._initialize(params))
        elif method == "ping":
            self._reply(msg_id, {})
        elif method == "tools/list":
            self._reply(msg_id, {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                for t in TOOL_SCHEMAS
            ]})
        elif method == "tools/call":
            self._pool.submit(self._call_tool_safely, msg_id, params)
        else:
            self._reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def _call_tool_safely(self, msg_id: Any, params: dict[str, Any]) -> None:
        try:
            self._call_tool(msg_id, params)
        except Exception as exc:  # Toolbox.run already catches tool errors; this guards the protocol layer
            self._reply(msg_id, error={"code": -32603, "message": f"{type(exc).__name__}: {exc}"})

    def serve(self, stdin: BinaryIO) -> None:
        for raw in iter(stdin.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for item in message if isinstance(message, list) else [message]:
                if isinstance(item, dict):
                    self.handle(item)
        self._pool.shutdown(wait=True)


def main() -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stdout carries the protocol; any stray print goes to stderr instead
    Server(Toolbox(), out).serve(sys.stdin.buffer)


if __name__ == "__main__":
    main()
