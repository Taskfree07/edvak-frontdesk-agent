"""One place for every model call: provider choice, retries, usage/cost accounting, budget and quota stops.

Callers build requests in the Anthropic Messages shape (system, messages, tools, output_config). The Anthropic
backend sends them as-is; the Gemini backend translates them. Both return a response with `.content` (blocks with
type text/tool_use), `.stop_reason` and normalised usage, so the understander, agent and reply drafter do not care
which provider is behind them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from frontdesk.config import Settings, price_usd
from frontdesk.events import EventBus, Stopwatch


class LLMError(RuntimeError):
    """The model could not produce a usable answer for this call. The pipeline falls back to rules mode."""


class SchemaRejected(LLMError):
    """The provider refused the tool/output schema. The agent retries once without strict schemas."""


class LLMStopped(LLMError):
    """No further model calls will be made this run: quota exhausted, budget reached or credentials invalid."""


class ModelUnavailable(LLMStopped):
    """This model cannot serve requests now (daily quota used up, retired, or overloaded after retries).
    Another model in the fallback chain may still work."""


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


class Content(list):
    """Response blocks, plus the provider's raw turn so it can be replayed verbatim (Gemini thought signatures)."""
    raw: Any = None


@dataclass
class Response:
    content: Content
    stop_reason: str
    usage: dict[str, int]
    model: str
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ Anthropic
class AnthropicBackend:
    def __init__(self) -> None:
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=3, timeout=120.0)
        self._fallbacks = True

    async def create(self, **kwargs: Any) -> Response:
        a = self.anthropic
        try:
            if self._fallbacks:
                try:
                    resp = await self.client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                except a.BadRequestError as e:
                    if "fallback" not in str(e).lower():
                        raise
                    self._fallbacks = False
                    resp = await self.client.messages.create(**kwargs)
            else:
                resp = await self.client.messages.create(**kwargs)
        except a.AuthenticationError as e:
            raise LLMStopped("Anthropic authentication failed - check ANTHROPIC_API_KEY.") from e
        except a.RateLimitError as e:
            raise LLMStopped(f"Anthropic rate limit / quota reached after retries: {str(e)[:200]}") from e
        except a.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise LLMStopped("Anthropic credit balance is exhausted.") from e
            raise SchemaRejected(f"Anthropic rejected the request: {str(e)[:300]}") from e
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {str(e)[:300]}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"Cannot reach the Anthropic API: {e}") from e
        u = resp.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        content = Content(resp.content)
        return Response(content, resp.stop_reason or "end_turn", usage, getattr(resp, "model", kwargs["model"]))


# ------------------------------------------------------------------------------------------------ Gemini
GEMINI_THINKING_BUDGET = {"low": 512, "medium": 2048, "high": 8192}
GEMINI_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
MAX_RATE_LIMIT_RETRIES = 4


def _json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini accepts JSON Schema for parameters; drop keys it does not use."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in ("$schema", "strict")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _quota_ids(details: Any) -> list[str]:
    """quotaId values from a Gemini RESOURCE_EXHAUSTED error, e.g. GenerateRequestsPerMinutePerProjectPerModel."""
    ids: list[str] = []
    for d in ((details or {}).get("error", {}) or {}).get("details", []) or []:
        for v in d.get("violations", []) or []:
            if v.get("quotaId"):
                ids.append(v["quotaId"])
    return ids


def _is_daily_quota(quota_ids: list[str], text: str) -> bool:
    if quota_ids:
        return any("PerDay" in q for q in quota_ids) and not any("PerMinute" in q for q in quota_ids)
    t = text.lower()
    return "perday" in t or "per day" in t or "daily" in t


def _retry_delay_s(err: Any, attempt: int) -> float:
    m = re.search(r"retry(?:Delay| in)[^0-9]*([0-9.]+)\s*s", str(err), re.IGNORECASE)
    return min(60.0, float(m.group(1)) + 1 if m else 2.0 * (2 ** attempt))


class GeminiBackend:
    def __init__(self) -> None:
        from google import genai
        from google.genai import errors, types
        self.types = types
        self.errors = errors
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key)

    def _config(self, model: str, kwargs: dict[str, Any]) -> Any:
        t = self.types
        system = kwargs.get("system")
        if isinstance(system, list):
            system = "\n\n".join(b.get("text", "") for b in system)
        cfg: dict[str, Any] = {"system_instruction": system or None,
                               "max_output_tokens": kwargs.get("max_tokens"),
                               "automatic_function_calling": t.AutomaticFunctionCallingConfig(disable=True)}
        oc = kwargs.get("output_config") or {}
        fmt = oc.get("format") or {}
        if fmt.get("type") == "json_schema":
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = _json_schema(fmt["schema"])
        effort = oc.get("effort")
        if effort:
            cfg["thinking_config"] = (t.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET.get(effort, 1024))
                                      if model.startswith("gemini-2.5") else t.ThinkingConfig(thinking_level=effort))
        tools = kwargs.get("tools")
        if tools:
            cfg["tools"] = [t.Tool(function_declarations=[
                t.FunctionDeclaration(name=x["name"], description=x.get("description", ""),
                                      parameters_json_schema=_json_schema(x["input_schema"])) for x in tools])]
        return t.GenerateContentConfig(**cfg)

    def _contents(self, messages: list[dict[str, Any]]) -> list[Any]:
        t = self.types
        names: dict[str, str] = {}
        out = []
        for m in messages:
            c = m["content"]
            if m["role"] == "assistant":
                if isinstance(c, Content) and c.raw is not None:
                    out.append(c.raw)
                    for b in c:
                        if b.type == "tool_use":
                            names[b.id] = b.name
                    continue
                c = [{"type": "text", "text": c}] if isinstance(c, str) else c
                out.append(t.Content(role="model", parts=[t.Part(text=b.text if hasattr(b, "text") else b["text"])
                                                          for b in c]))
                continue
            if isinstance(c, str):
                out.append(t.Content(role="user", parts=[t.Part(text=c)]))
                continue
            parts = []
            for b in c:
                if b.get("type") == "tool_result":
                    raw = b.get("content")
                    try:
                        val = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        val = raw
                    resp = {"error": val} if b.get("is_error") else {"result": val}
                    parts.append(t.Part(function_response=t.FunctionResponse(
                        id=b["tool_use_id"], name=names.get(b["tool_use_id"], "unknown"), response=resp)))
                else:
                    parts.append(t.Part(text=b.get("text", "")))
            out.append(t.Content(role="user", parts=parts))
        return out

    async def create(self, **kwargs: Any) -> Response:
        model = kwargs["model"]
        config = self._config(model, kwargs)
        contents = self._contents(kwargs["messages"])
        e_mod = self.errors
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                resp = await self.client.aio.models.generate_content(model=model, contents=contents, config=config)
                break
            except e_mod.APIError as e:
                text = f"{e.status} {e.message} {e.details}"
                if e.code == 429:
                    ids = _quota_ids(e.details)
                    if _is_daily_quota(ids, text):
                        raise ModelUnavailable(f"Gemini daily quota exhausted for {model} "
                                             f"({', '.join(ids) or 'per-day limit'}). It resets daily; "
                                             "or use a paid key.") from e
                    if attempt < MAX_RATE_LIMIT_RETRIES:
                        await asyncio.sleep(_retry_delay_s(text, attempt))
                        continue
                    raise LLMStopped(f"Gemini rate limit persisted after {attempt + 1} attempts: "
                                     f"{str(e.message)[:200]}") from e
                if e.code in (401, 403) or "API key" in text:
                    raise LLMStopped(f"Gemini rejected the API key ({e.code}): {str(e.message)[:200]}") from e
                if e.code == 400:
                    raise SchemaRejected(f"Gemini rejected the request: {str(e.message)[:300]}") from e
                if e.code >= 500 and attempt < 3:
                    await asyncio.sleep(3.0 * (attempt + 1))
                    continue
                if e.code == 404 or e.code >= 500:
                    raise ModelUnavailable(f"Gemini model {model} unavailable ({e.code}): {str(e.message)[:200]}") from e
                raise LLMError(f"Gemini API error {e.code}: {str(e.message)[:300]}") from e
            except Exception as e:  # network / SDK errors
                raise LLMError(f"Cannot reach the Gemini API: {e!r}"[:300]) from e
        return self._response(model, resp)

    def _response(self, model: str, resp: Any) -> Response:
        um = resp.usage_metadata
        cached = (um.cached_content_token_count or 0) if um else 0
        usage = {"input_tokens": max(0, ((um.prompt_token_count or 0) if um else 0) - cached),
                 "output_tokens": ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
        content = Content()
        cand = (resp.candidates or [None])[0]
        finish = str(getattr(cand, "finish_reason", "") or "").split(".")[-1]
        blocked = resp.prompt_feedback is not None and resp.prompt_feedback.block_reason is not None
        if cand is not None and cand.content is not None:
            content.raw = cand.content
            for i, p in enumerate(cand.content.parts or []):
                if p.function_call is not None:
                    fc = p.function_call
                    content.append(Block("tool_use", id=fc.id or f"call_{i}_{int(time.time() * 1000)}",
                                         name=fc.name, input=dict(fc.args or {})))
                elif p.text and not p.thought:
                    content.append(Block("text", text=p.text))
        if blocked or finish in GEMINI_REFUSALS:
            stop = "refusal"
        elif any(b.type == "tool_use" for b in content):
            stop = "tool_use"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        return Response(content, stop, usage, resp.model_version or model, {"finish_reason": finish})


# ------------------------------------------------------------------------------------------------ facade
class LLM:
    def __init__(self, settings: Settings, bus: EventBus):
        self.settings = settings
        self.bus = bus
        self.backend = GeminiBackend() if settings.provider == "gemini" else AnthropicBackend()
        self.totals: dict[str, float] = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0, "cost_usd": 0.0, "calls": 0}
        self.stopped: str | None = None
        same_provider = [m for m in settings.model_fallbacks
                         if m != settings.model and m.startswith("gemini") == (settings.provider == "gemini")]
        self.models: list[str] = [settings.model] + same_provider
        self.switches: list[dict[str, str]] = []
        self._min_interval = 60.0 / settings.max_rpm if settings.max_rpm else 0.0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    def _stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason
            self.bus.emit("llm.stopped", None, reason=reason, totals=dict(self.totals))

    async def _throttle(self) -> None:
        if not self._min_interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def create(self, message_id: str | None, purpose: str, **kwargs: Any) -> Response:
        if self.stopped:
            raise LLMStopped(self.stopped)
        while True:
            kwargs["model"] = self.models[0]
            await self._throttle()
            sw = Stopwatch()
            try:
                resp = await self.backend.create(**kwargs)
                break
            except ModelUnavailable as e:
                if len(self.models) > 1:
                    old = self.models.pop(0)
                    self.switches.append({"from": old, "to": self.models[0], "reason": str(e)})
                    self.bus.emit("llm.model_switched", None, from_model=old, to_model=self.models[0],
                                  reason=str(e), during_message=message_id)
                    continue
                self._stop(str(e))
                raise
            except LLMStopped as e:
                self._stop(str(e))
                raise
        cost = price_usd(kwargs["model"], resp.usage)
        for k, v in resp.usage.items():
            self.totals[k] += v
        self.totals["cost_usd"] += cost
        self.totals["calls"] += 1
        self.bus.emit("llm.usage", message_id, purpose=purpose, model=resp.model, stop_reason=resp.stop_reason,
                      duration_ms=sw.ms, cost_usd=round(cost, 6), **resp.usage)
        s = self.settings
        tokens = self.totals["input_tokens"] + self.totals["output_tokens"] + self.totals["cache_read_input_tokens"]
        if s.max_cost_usd and self.totals["cost_usd"] >= s.max_cost_usd:
            self._stop(f"Run budget reached: ${self.totals['cost_usd']:.4f} >= FRONTDESK_MAX_COST_USD "
                       f"${s.max_cost_usd:.2f}")
        elif s.max_tokens and tokens >= s.max_tokens:
            self._stop(f"Run token budget reached: {int(tokens):,} >= FRONTDESK_MAX_TOKENS {s.max_tokens:,}")
        if resp.stop_reason == "refusal":
            raise LLMError(f"Model declined ({purpose}).")
        return resp

    @staticmethod
    def text_of(resp: Any) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text" and b.text)
