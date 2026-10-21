"""ToolClient: the agent's only door to the clinic. Talks MCP (in-process or stdio subprocess), records every
call in a per-message ledger, injects the message context id, and emits trace events."""

from __future__ import annotations

import copy
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters

from frontdesk.config import ROOT, Settings
from frontdesk.data import ClinicData
from frontdesk.events import EventBus, Stopwatch
from frontdesk.tools.mcp_server import ORCHESTRATOR_ONLY, build_server
from frontdesk.tools.service import ClinicService

INJECTED = {"context_id"}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    by: str                      # "llm" | "orchestrator" | "code" (policy enforcement) | "rules"
    ms: float

    @property
    def ok(self) -> bool:
        return bool(self.result.get("ok"))


@dataclass
class Ledger:
    message_id: str
    context_id: str | None = None
    calls: list[ToolCall] = field(default_factory=list)

    def of(self, name: str, ok: bool | None = None) -> list[ToolCall]:
        return [c for c in self.calls if c.name == name and (ok is None or c.ok == ok)]

    def names(self) -> list[str]:
        return [c.name for c in self.calls]


def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """MCP JSON schema -> Anthropic strict tool schema: drop injected params, titles and defaults."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            for k in list(node):
                node[k] = walk(node[k])
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
        elif isinstance(node, list):
            return [walk(x) for x in node]
        return node

    s = walk(s)
    props = s.get("properties", {})
    for k in INJECTED:
        props.pop(k, None)
    s["required"] = [r for r in s.get("required", []) if r not in INJECTED]
    s["properties"] = props
    s["additionalProperties"] = False
    return s


class ToolClient:
    def __init__(self, settings: Settings, bus: EventBus, data: ClinicData | None = None):
        self.settings = settings
        self.bus = bus
        self.data = data or ClinicData.load(settings.data_dir)
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self.service: ClinicService | None = None   # set in-process only (tests may inspect it)
        self._tools: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "ToolClient":
        self._stack = AsyncExitStack()
        if self.settings.transport == "stdio":
            env = {**os.environ, "FRONTDESK_DATA_DIR": str(self.settings.data_dir), "PYTHONPATH": str(ROOT)}
            server = StdioServerParameters(command=sys.executable, args=["-m", "frontdesk.tools.mcp_server"], env=env)
        else:
            self.service = ClinicService(self.data)
            server = build_server(self.service)
        self._client = await self._stack.enter_async_context(Client(server))
        listed = await self._client.list_tools()
        self._tools = {t.name: {"description": t.description or "", "input_schema": t.input_schema}
                       for t in listed.tools}
        self.bus.emit("mcp.connected", None, transport=self.settings.transport, tools=sorted(self._tools))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def llm_tools(self, names: list[str], strict: bool = True) -> list[dict[str, Any]]:
        """Anthropic tool definitions for the given MCP tools (never orchestrator-only ones)."""
        out = []
        for n in names:
            if n in ORCHESTRATOR_ONLY or n not in self._tools:
                continue
            t = {"name": n, "description": self._tools[n]["description"],
                 "input_schema": _sanitize_schema(self._tools[n]["input_schema"])}
            if strict:
                t["strict"] = True
            out.append(t)
        return out

    async def call(self, ledger: Ledger, name: str, args: dict[str, Any], by: str) -> dict[str, Any]:
        """Call an MCP tool. context_id is injected here; callers (incl. the LLM) never supply it."""
        if by == "llm" and name in ORCHESTRATOR_ONLY:
            result = {"ok": False, "reason": "not_permitted", "message": f"{name} is not available to the agent."}
            ledger.calls.append(ToolCall(name, args, result, by, 0.0))
            return result
        full = dict(args)
        schema_props = self._tools.get(name, {}).get("input_schema", {}).get("properties", {})
        if "context_id" in schema_props:
            full["context_id"] = ledger.context_id or ""
        self.bus.emit("tool.called", ledger.message_id, stage="act", tool=name, args=args, by=by)
        sw = Stopwatch()
        if name not in self._tools:
            result = {"ok": False, "reason": "unknown_tool", "message": f"No tool named {name}."}
        else:
            try:
                r = await self._client.call_tool(name, full)
                if r.structured_content is not None:
                    result = dict(r.structured_content)
                else:
                    text = "".join(getattr(c, "text", "") for c in r.content)
                    try:
                        result = json.loads(text)
                    except json.JSONDecodeError:
                        result = {"ok": not r.is_error, "text": text}
                if r.is_error:
                    result.setdefault("ok", False)
            except Exception as e:  # transport failure: report, never crash the run
                result = {"ok": False, "reason": "tool_error", "message": str(e)[:300]}
        call = ToolCall(name, args, result, by, sw.ms)
        ledger.calls.append(call)
        self.bus.emit("tool.result", ledger.message_id, stage="act", duration_ms=call.ms, tool=name, by=by,
                      ok=call.ok, result=result)
        return result
