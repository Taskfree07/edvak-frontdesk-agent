"""One place for every model call: provider chain, timeouts, retries, usage/cost accounting, budget and quota stops.

Callers build requests in the Anthropic Messages shape (system, messages, tools, output_config). The Anthropic
backend sends them as-is; the Gemini and Groq backends translate them. All return a response with `.content`
(blocks with type text/tool_use), `.stop_reason` and normalised usage, so the understander, agent and reply drafter
do not care which provider is behind them.

Provider chain: the primary model plus FRONTDESK_MODEL_FALLBACKS. Gemini and Groq models can be mixed (Groq ids are
prefixed "groq:"); Claude only chains with Claude. A transient failure (timeout, network, overload, per-minute
limit that does not fit the deadline) moves THIS call to the next model; a permanent one (daily quota, retired
model, rejected key) removes the model for the rest of the run.

Deadlines: every call is bounded by FRONTDESK_CALL_TIMEOUT_S and by the current message deadline (set by the
pipeline with `message_deadline`). Retries never wait past the deadline.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from frontdesk.config import Settings, price_usd, provider_of
from frontdesk.events import EventBus, Stopwatch


class LLMError(RuntimeError):
    """The model could not produce a usable answer for this call. The pipeline falls back to rules mode."""


class SchemaRejected(LLMError):
    """The provider refused the tool/output schema. The agent retries once without strict schemas."""


class DeadlineExceeded(LLMError):
    """The message's time budget ran out before a usable model answer arrived."""


class LLMStopped(LLMError):
    """No further model calls will be made this run: every model exhausted, budget reached."""


class ModelUnavailable(LLMStopped):
    """This model cannot serve this call. permanent=True: drop it for the rest of the run (daily quota, retired,
    rejected key). permanent=False: try the next model for this call only (timeout, network, overload)."""

    def __init__(self, message: str, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


class Content(list):
    """Response blocks, plus the provider's raw turn so the same provider can replay it verbatim (Gemini thought
    signatures). Another provider rebuilds the turn from the blocks."""
    raw: Any = None
    provider: str | None = None


@dataclass
class Response:
    content: Content
    stop_reason: str
    usage: dict[str, int]
    model: str
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ deadlines
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("frontdesk_deadline", default=None)


@contextmanager
def message_deadline(seconds: float | None) -> Iterator[None]:
    """Bound every model call made inside this block (in this task) to `seconds` from now."""
    token = _deadline.set(time.monotonic() + seconds if seconds else None)
    try:
        yield
    finally:
        _deadline.reset(token)


_progress: contextvars.ContextVar[Any] = contextvars.ContextVar("frontdesk_progress", default=None)


def _note_retry(model: str, reason: str, wait_s: float) -> None:
    cb = _progress.get()
    if cb:
        cb(model, reason, wait_s)


def deadline_active() -> bool:
    return _deadline.get() is not None


def _remaining() -> float | None:
    d = _deadline.get()
    return None if d is None else d - time.monotonic()


def _fits(wait_s: float) -> bool:
    r = _remaining()
    return r is None or wait_s < r - 1.0


def _block_get(b: Any, key: str) -> Any:
    return b.get(key) if isinstance(b, dict) else getattr(b, key, None)


def _json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the OpenAI-style / Gemini schema dialects do not use."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in ("$schema", "strict")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _system_text(system: Any) -> str | None:
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system)
    return system or None


# ------------------------------------------------------------------------------------------------ Anthropic
class AnthropicBackend:
    provider = "anthropic"

    def __init__(self) -> None:
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=1, timeout=120.0)
        self._fallbacks = True

    async def create(self, timeout_s: float, **kwargs: Any) -> Response:
        a = self.anthropic
        client = self.client.with_options(timeout=timeout_s)
        try:
            if self._fallbacks:
                try:
                    resp = await client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                except a.BadRequestError as e:
                    if "fallback" not in str(e).lower():
                        raise
                    self._fallbacks = False
                    resp = await client.messages.create(**kwargs)
            else:
                resp = await client.messages.create(**kwargs)
        except a.AuthenticationError as e:
            raise ModelUnavailable("Anthropic rejected the API key - check ANTHROPIC_API_KEY.") from e
        except a.RateLimitError as e:
            raise ModelUnavailable(f"Anthropic rate limit / quota reached: {str(e)[:200]}", permanent=False) from e
        except a.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise ModelUnavailable("Anthropic credit balance is exhausted.") from e
            raise SchemaRejected(f"Anthropic rejected the request: {str(e)[:300]}") from e
        except a.APITimeoutError as e:
            raise ModelUnavailable(f"Anthropic call timed out after {timeout_s:.0f} s", permanent=False) from e
        except a.APIStatusError as e:
            if e.status_code >= 500:
                raise ModelUnavailable(f"Anthropic API error {e.status_code}", permanent=False) from e
            raise LLMError(f"Anthropic API error {e.status_code}: {str(e)[:300]}") from e
        except a.APIConnectionError as e:
            raise ModelUnavailable(f"Cannot reach the Anthropic API: {e}", permanent=False) from e
        u = resp.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        content = Content(resp.content)
        content.provider = self.provider
        return Response(content, resp.stop_reason or "end_turn", usage, getattr(resp, "model", kwargs["model"]))


# ------------------------------------------------------------------------------------------------ Gemini
GEMINI_THINKING_BUDGET = {"low": 512, "medium": 2048, "high": 8192}
GEMINI_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
GEMINI_FOREIGN_SIGNATURE = b"skip_thought_signature_validator"   # documented placeholder for replayed foreign calls


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
    provider = "gemini"
    max_wait = 3.0      # longest rate-limit wait before moving to the next model

    def __init__(self) -> None:
        from google import genai
        from google.genai import errors, types
        self.types = types
        self.errors = errors
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key)

    def _config(self, model: str, kwargs: dict[str, Any]) -> Any:
        t = self.types
        cfg: dict[str, Any] = {"system_instruction": _system_text(kwargs.get("system")),
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
                if isinstance(c, Content) and c.raw is not None and c.provider == self.provider:
                    out.append(c.raw)
                    for b in c:
                        if b.type == "tool_use":
                            names[b.id] = b.name
                    continue
                blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c
                parts = []
                for b in blocks:
                    if _block_get(b, "type") == "tool_use":
                        names[_block_get(b, "id")] = _block_get(b, "name")
                        parts.append(t.Part(function_call=t.FunctionCall(
                            id=_block_get(b, "id"), name=_block_get(b, "name"), args=_block_get(b, "input") or {}),
                            thought_signature=GEMINI_FOREIGN_SIGNATURE))
                    elif _block_get(b, "text"):
                        parts.append(t.Part(text=_block_get(b, "text")))
                out.append(t.Content(role="model", parts=parts or [t.Part(text="")]))
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

    async def create(self, timeout_s: float, **kwargs: Any) -> Response:
        model = kwargs["model"]
        config = self._config(model, kwargs)
        contents = self._contents(kwargs["messages"])
        e_mod = self.errors
        attempt = 0
        while True:
            try:
                # Enforced here, not via the SDK's http timeout: the Gemini API rejects deadlines under 10 s.
                limit = timeout_s if _remaining() is None else max(0.5, min(timeout_s, _remaining()))
                resp = await asyncio.wait_for(
                    self.client.aio.models.generate_content(model=model, contents=contents, config=config), limit)
                break
            except e_mod.APIError as e:
                text = f"{e.status} {e.message} {e.details}"
                if e.code == 429:
                    ids = _quota_ids(e.details)
                    if _is_daily_quota(ids, text):
                        raise ModelUnavailable(f"Gemini daily quota exhausted for {model} "
                                               f"({', '.join(ids) or 'per-day limit'}).") from e
                    delay = _retry_delay_s(text, attempt)
                    if attempt < 3 and delay <= self.max_wait and _fits(delay):
                        attempt += 1
                        _note_retry(model, "per-minute rate limit", delay)
                        await asyncio.sleep(delay)
                        continue
                    raise ModelUnavailable(f"Gemini per-minute limit on {model}; retry in {delay:.0f} s is too long",
                                           permanent=False) from e
                if e.code in (401, 403) or "API key" in text:
                    raise ModelUnavailable(f"Gemini rejected the API key ({e.code})") from e
                if e.code == 400:
                    raise SchemaRejected(f"Gemini rejected the request: {str(e.message)[:300]}") from e
                if e.code == 404:
                    raise ModelUnavailable(f"Gemini model {model} unavailable (404): {str(e.message)[:150]}") from e
                if e.code >= 500:
                    if attempt < 1 and _fits(2.0):
                        attempt += 1
                        _note_retry(model, f"busy ({e.code})", 2.0)
                        await asyncio.sleep(2.0)
                        continue
                    raise ModelUnavailable(f"Gemini {model} overloaded ({e.code})", permanent=False) from e
                raise LLMError(f"Gemini API error {e.code}: {str(e.message)[:300]}") from e
            except Exception as e:  # network errors, timeouts, SDK errors
                timed_out = isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower()
                if not timed_out and attempt < 1 and _fits(1.0):
                    attempt += 1
                    _note_retry(model, type(e).__name__, 1.0)
                    await asyncio.sleep(1.0)
                    continue
                kind = f"timed out after {limit:.0f} s" if timed_out else "network error"
                raise ModelUnavailable(f"Gemini {model} {kind}: {type(e).__name__}", permanent=False) from e
        return self._response(model, resp)

    def _response(self, model: str, resp: Any) -> Response:
        um = resp.usage_metadata
        cached = (um.cached_content_token_count or 0) if um else 0
        usage = {"input_tokens": max(0, ((um.prompt_token_count or 0) if um else 0) - cached),
                 "output_tokens": ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
        content = Content()
        content.provider = self.provider
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


# ------------------------------------------------------------------------------------------------ Groq
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqBackend:
    """Groq's OpenAI-compatible chat completions API over httpx. Structured output and tool calling are never
    combined in one request (Groq does not support that); this application never needs both at once."""
    provider = "groq"
    max_wait = 3.0

    def __init__(self) -> None:
        import httpx
        self.httpx = httpx
        self._key = os.getenv("GROQ_API_KEY", "")
        self.client = httpx.AsyncClient(base_url=GROQ_BASE_URL)

    def _messages(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        system = _system_text(kwargs.get("system"))
        if system:
            out.append({"role": "system", "content": system})
        for m in kwargs["messages"]:
            c = m["content"]
            if m["role"] == "assistant":
                blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c
                text = "".join(_block_get(b, "text") or "" for b in blocks if _block_get(b, "type") == "text")
                calls = [{"id": _block_get(b, "id"), "type": "function",
                          "function": {"name": _block_get(b, "name"),
                                       "arguments": json.dumps(_block_get(b, "input") or {})}}
                         for b in blocks if _block_get(b, "type") == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            elif isinstance(c, str):
                out.append({"role": "user", "content": c})
            else:
                texts = []
                for b in c:
                    if b.get("type") == "tool_result":
                        content = b.get("content")
                        content = content if isinstance(content, str) else json.dumps(content, default=str)
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                    "content": ("ERROR: " if b.get("is_error") else "") + content})
                    elif b.get("text"):
                        texts.append(b["text"])
                if texts:
                    out.append({"role": "user", "content": "\n".join(texts)})
        return out

    def _body(self, model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": self._messages(kwargs)}
        if kwargs.get("max_tokens"):
            body["max_completion_tokens"] = kwargs["max_tokens"]
        oc = kwargs.get("output_config") or {}
        fmt = oc.get("format") or {}
        tools = kwargs.get("tools")
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": x["name"],
                                                               "description": x.get("description", ""),
                                                               "parameters": _json_schema(x["input_schema"])}}
                             for x in tools]
            body["tool_choice"] = "auto"
        elif fmt.get("type") == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "schema": _json_schema(fmt["schema"])}}
        if model.startswith("qwen/"):
            body["reasoning_format"] = "hidden"
        elif model.startswith("openai/gpt-oss"):
            body["include_reasoning"] = False
            if oc.get("effort") in ("low", "medium", "high"):
                body["reasoning_effort"] = oc["effort"]
        return body

    async def create(self, timeout_s: float, **kwargs: Any) -> Response:
        public = kwargs["model"]
        model = public.split(":", 1)[1]
        body = self._body(model, kwargs)
        headers = {"Authorization": f"Bearer {self._key}"}
        attempt = 0
        while True:
            try:
                r = await self.client.post("/chat/completions", json=body, headers=headers, timeout=timeout_s)
            except (self.httpx.TimeoutException, self.httpx.TransportError) as e:
                if not isinstance(e, self.httpx.TimeoutException) and attempt < 1 and _fits(1.0):
                    attempt += 1
                    _note_retry(public, type(e).__name__, 1.0)
                    await asyncio.sleep(1.0)
                    continue
                kind = "timed out" if isinstance(e, self.httpx.TimeoutException) else "network error"
                raise ModelUnavailable(f"Groq {model} {kind}: {type(e).__name__}", permanent=False) from e
            if r.status_code == 200:
                return self._response(public, r.json())
            msg = _groq_error(r)
            if r.status_code in (401, 403):
                raise ModelUnavailable(f"Groq rejected the API key ({r.status_code})")
            if r.status_code == 404:
                raise ModelUnavailable(f"Groq model {model} unavailable (404): {msg[:150]}")
            if r.status_code == 429:
                if "per day" in msg.lower() or "(rpd)" in msg.lower() or "(tpd)" in msg.lower():
                    raise ModelUnavailable(f"Groq daily limit reached for {model}: {msg[:150]}")
                delay = float(r.headers.get("retry-after") or 2.0) + 0.5
                if attempt < 2 and delay <= self.max_wait and _fits(delay):
                    attempt += 1
                    _note_retry(public, "per-minute rate limit", delay)
                    await asyncio.sleep(delay)
                    continue
                raise ModelUnavailable(f"Groq per-minute limit on {model}; retry in {delay:.0f} s is too long",
                                       permanent=False)
            if r.status_code == 400:
                raise SchemaRejected(f"Groq rejected the request: {msg[:300]}")
            if r.status_code >= 500:
                if attempt < 1 and _fits(1.0):
                    attempt += 1
                    _note_retry(public, f"busy ({r.status_code})", 1.0)
                    await asyncio.sleep(1.0)
                    continue
                raise ModelUnavailable(f"Groq {model} unavailable ({r.status_code})", permanent=False)
            raise LLMError(f"Groq API error {r.status_code}: {msg[:300]}")

    def _response(self, public_model: str, data: dict[str, Any]) -> Response:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = Content()
        content.provider = self.provider
        if message.get("content"):
            content.append(Block("text", text=message["content"]))
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append(Block("tool_use", id=tc.get("id") or f"call_{i}", name=fn.get("name"),
                                 input=args if isinstance(args, dict) else {}))
        finish = choice.get("finish_reason") or ""
        if any(b.type == "tool_use" for b in content):
            stop = "tool_use"
        elif finish == "length":
            stop = "max_tokens"
        elif finish == "content_filter":
            stop = "refusal"
        else:
            stop = "end_turn"
        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage = {"input_tokens": max(0, (u.get("prompt_tokens") or 0) - cached),
                 "output_tokens": u.get("completion_tokens") or 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
        return Response(content, stop, usage, public_model, {"finish_reason": finish})


def _groq_error(r: Any) -> str:
    try:
        return str((r.json().get("error") or {}).get("message") or r.text)
    except Exception:
        return r.text


BACKENDS = {"anthropic": AnthropicBackend, "gemini": GeminiBackend, "groq": GroqBackend}
PROVIDER_KEYS = {"anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                 "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "groq": ("GROQ_API_KEY",)}


def has_key(provider: str) -> bool:
    return any(os.getenv(k) for k in PROVIDER_KEYS[provider])


# ------------------------------------------------------------------------------------------------ facade
class LLM:
    def __init__(self, settings: Settings, bus: EventBus):
        self.settings = settings
        self.bus = bus
        primary = settings.provider
        chain = [settings.model] + [m for m in settings.model_fallbacks if m != settings.model]
        if primary == "anthropic":
            chain = [m for m in chain if provider_of(m) == "anthropic"]
        else:
            chain = [m for m in chain if provider_of(m) != "anthropic"]
        self.models: list[str] = [m for i, m in enumerate(chain) if i == 0 or has_key(provider_of(m))]
        self._backends: dict[str, Any] = {}
        self.totals: dict[str, float] = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0, "cost_usd": 0.0, "calls": 0}
        self.stopped: str | None = None
        self.switches: list[dict[str, str]] = []
        self._min_interval = 60.0 / settings.max_rpm if settings.max_rpm else 0.0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> Any:
        """Backend of the current first model (kept for callers/tests that set a single backend)."""
        return self._backend_for(self.models[0])

    @backend.setter
    def backend(self, value: Any) -> None:
        self._backends = {"*": value}

    def _backend_for(self, model: str) -> Any:
        if "*" in self._backends:
            return self._backends["*"]
        p = provider_of(model)
        if p not in self._backends:
            self._backends[p] = BACKENDS[p]()
            self._backends[p].max_wait = self.settings.max_retry_wait_s
        return self._backends[p]

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
        await self._throttle()
        last: Exception | None = None
        for model in list(self.models):
            remaining = _remaining()
            if remaining is not None and remaining <= 0.5:
                self.bus.emit("llm.deadline", message_id, purpose=purpose,
                              deadline_s=self.settings.message_deadline_s)
                raise DeadlineExceeded(f"message deadline of {self.settings.message_deadline_s:.0f} s reached "
                                       f"before {purpose} completed")
            timeout_s = self.settings.call_timeout_s if remaining is None else min(self.settings.call_timeout_s,
                                                                                 max(1.0, remaining))
            kwargs["model"] = model
            sw = Stopwatch()
            token = _progress.set(lambda m, why, wait: self.bus.emit(
                "llm.retry", None, model=m, reason=why, wait_s=round(wait, 1), purpose=purpose,
                during_message=message_id))
            try:
                resp = await self._backend_for(model).create(timeout_s=timeout_s, **kwargs)
            except ModelUnavailable as e:
                last = e
                nxt = next((m for m in self.models if m != model and self.models.index(m) > self.models.index(model)),
                           None)
                if e.permanent:
                    self.models.remove(model)
                self.switches.append({"from": model, "to": nxt or "-", "reason": str(e),
                                      "permanent": str(e.permanent)})
                self.bus.emit("llm.model_switched", None, from_model=model, to_model=nxt, reason=str(e),
                              permanent=e.permanent, during_message=message_id, purpose=purpose,
                              failed_after_ms=round(sw.ms))
                continue
            finally:
                _progress.reset(token)
            return self._account(message_id, purpose, model, resp, sw.ms)
        if not self.models:
            self._stop(f"All models exhausted; last error: {last}")
            raise LLMStopped(self.stopped or "All models exhausted")
        raise LLMError(f"No model could answer {purpose}: {last}")

    def _account(self, message_id: str | None, purpose: str, model: str, resp: Response, ms: float) -> Response:
        cost = price_usd(model, resp.usage)
        for k, v in resp.usage.items():
            self.totals[k] += v
        self.totals["cost_usd"] += cost
        self.totals["calls"] += 1
        self.bus.emit("llm.usage", message_id, purpose=purpose, model=model, provider=provider_of(model),
                      stop_reason=resp.stop_reason, latency_ms=round(ms), cost_usd=round(cost, 6), **resp.usage)
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
