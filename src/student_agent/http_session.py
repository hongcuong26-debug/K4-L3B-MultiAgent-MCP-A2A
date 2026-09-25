"""Minimal Streamable HTTP MCP session for servers with finite POST/SSE responses.

This transport uses pooled synchronous HTTP; SDK types still validate JSON-RPC results.
It deliberately does not implement subscriptions, sampling or server-initiated tool calls.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import httpx2
from mcp import types


class HttpSession:
    def __init__(self, endpoint: str, team_api_key: str) -> None:
        self.endpoint = endpoint
        self.headers = {
            "Authorization": f"Bearer {team_api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._next_id = 0
        self._client = httpx2.Client(timeout=20, follow_redirects=False)

    async def initialize(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "day09-student-agent", "version": "0.1.0"},
            },
        )
        if result.get("protocolVersion") not in {"2025-03-26", "2025-06-18", "2024-11-05"}:
            raise ValueError("Unsupported MCP protocol version")
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        await self.request("notifications/initialized", {}, notification=True)

    async def request(self, method: str, params: dict, *, notification: bool = False) -> Any:
        # Keep each finite request bounded; no persistent server event stream is needed.
        # Incrementing is synchronous on the event loop, so each concurrent request gets
        # a distinct JSON-RPC id. The pooled httpx client can then run the calls in parallel.
        self._next_id += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self._next_id
        return await asyncio.to_thread(self._post, message)

    def _post(self, message: dict) -> Any:
        # Read the finite body completely so the TCP connection can be reused.
        response = self._client.post(self.endpoint, json=message, headers=self.headers)
        if response.status_code in {401, 403}:
            raise PermissionError("MCP authentication failed; check the team key and run")
        if response.status_code >= 300:
            raise RuntimeError(f"MCP HTTP status {response.status_code}")
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
        if "id" not in message:
            return None
        if len(response.content) > 1024 * 1024:
            raise ValueError("MCP response exceeds 1 MB")
        content_type = response.headers.get("content-type", "").split(";")[0]
        if content_type == "application/json":
            payload = response.json()
        elif content_type == "text/event-stream":
            payload = self._read_sse(io.BytesIO(response.content), message["id"])
        else:
            raise ValueError("Unsupported MCP response content type")
        if payload.get("id") != message["id"]:
            raise ValueError("MCP response correlation mismatch")
        if "error" in payload:
            raise RuntimeError("MCP request returned a JSON-RPC error")
        return payload["result"]

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _read_sse(response, request_id: int) -> dict:
        data: list[str] = []
        size = 0
        for raw_line in response:
            size += len(raw_line)
            if size > 1024 * 1024:
                raise ValueError("MCP response exceeds 1 MB")
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line and data:
                payload = json.loads("\n".join(data))
                if payload.get("id") == request_id:
                    return payload
                data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        raise ValueError("MCP stream ended without a matching response")

    async def list_tools(self, *, params=None):
        arguments = params.model_dump(by_alias=True, exclude_none=True) if params else {}
        return types.ListToolsResult.model_validate(await self.request("tools/list", arguments))

    async def call_tool(self, name: str, arguments: dict):
        return types.CallToolResult.model_validate(
            await self.request("tools/call", {"name": name, "arguments": arguments})
        )
