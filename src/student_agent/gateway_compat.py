"""Bridge the pooled HTTP transport to the camel-case MCP API used by mcp_gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator
from mcp import types

from .contracts import Contracts
from .http_session import HttpSession
from .mcp_gateway import EvidenceGateway


class GatewayCompatibleSession:
    """Expose SDK-v1-style result attributes without editing the locked gateway."""

    def __init__(self, session: HttpSession) -> None:
        self._session = session
        self._tool_schemas: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        await self._session.initialize()

    async def list_tools(self) -> SimpleNamespace:
        found: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor is not None else {}
            page = types.ListToolsResult.model_validate(
                await self._session.request("tools/list", params)
            )
            for tool in page.tools:
                schema = getattr(tool, "inputSchema", None)
                if schema is None:
                    schema = getattr(tool, "input_schema", None)
                if not isinstance(schema, dict):
                    raise ValueError(f"MCP tool {tool.name} has no valid input schema")
                Draft202012Validator.check_schema(schema)
                self._tool_schemas[tool.name] = schema
                found.append(SimpleNamespace(name=tool.name))

            cursor = getattr(page, "nextCursor", None)
            if cursor is None:
                cursor = getattr(page, "next_cursor", None)
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise ValueError("MCP tools/list repeated a pagination cursor")
            seen_cursors.add(cursor)
        return SimpleNamespace(tools=found)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        schema = self._tool_schemas.get(name)
        if schema is None:
            raise ValueError(f"MCP tool {name} was not discovered")
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ValueError(f"MCP tool {name} arguments:{location}: {error.message}")

        result = types.CallToolResult.model_validate(
            await self._session.request(
                "tools/call", {"name": name, "arguments": arguments}
            )
        )
        # The installed SDK exposes snake_case fields while the preserved gateway reads
        # camelCase. Provide both spellings at this adapter boundary.
        return SimpleNamespace(
            isError=getattr(result, "isError", getattr(result, "is_error", False)),
            structuredContent=getattr(
                result, "structuredContent", getattr(result, "structured_content", None)
            ),
            structured_content=getattr(
                result, "structured_content", getattr(result, "structuredContent", None)
            ),
            content=result.content,
        )

    def close(self) -> None:
        self._session.close()


@asynccontextmanager
async def connect_compatible_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    session = GatewayCompatibleSession(HttpSession(endpoint, team_api_key))
    try:
        await session.initialize()
        yield EvidenceGateway(session, contracts)  # type: ignore[arg-type]
    finally:
        session.close()
