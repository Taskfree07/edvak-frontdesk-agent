"""Provider chain, timeouts, deadlines and safe degradation, with mocked providers (no network)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import httpx
import pytest

from frontdesk.config import Settings
from frontdesk.data import Message
from frontdesk.events import EventBus, ListSink
from frontdesk.llm import (LLM, Content, DeadlineExceeded, GroqBackend, LLMError, LLMStopped, ModelUnavailable,
                           Response, message_deadline)
from frontdesk.pipeline import Pipeline
from frontdesk.tools.client import ToolClient


def ok(model="m"):
    return Response(Content(), "end_turn", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                                            "cache_creation_input_tokens": 0}, model)


class ScriptedBackend:
    """Per-model script: each entry is an exception to raise, a Response, or ("sleep", seconds)."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    async def create(self, timeout_s, **kwargs):
        model = kwargs["model"]
        self.calls.append((model, round(timeout_s, 1)))
        item = self.scripts[model].pop(0)
        if isinstance(item, tuple) and item[0] == "sleep":
            await asyncio.sleep(item[1])
            raise ModelUnavailable(f"{model} timed out", permanent=False)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(models, scripts, **settings):
    s = Settings(model=models[0], model_fallbacks=models[1:], max_cost_usd=None, **settings)
    bus = EventBus()
    sink = ListSink()
    bus.subscribe(sink)
    llm = LLM(s, bus)
    llm.models = list(models)          # do not depend on which API keys exist on this machine
    backend = ScriptedBackend(scripts)
    llm.backend = backend
    return llm, backend, sink


G, Q, X = "gemini-3.5-flash-lite", "groq:qwen/qwen3.8-27b", "groq:openai/gpt-oss-120b"


def test_timeout_moves_this_call_to_groq_and_keeps_gemini_for_later():
    llm, be, sink = make_llm([G, Q], {G: [ModelUnavailable("timed out", permanent=False), ok(G)], Q: [ok(Q)]})
    asyncio.run(llm.create("M1", "understand", messages=[]))
    assert [c[0] for c in be.calls] == [G, Q]
    assert llm.models == [G, Q]                                    # transient: Gemini stays in the chain
    llm._cooling[G] = 0                                            # once its cool-down has passed
    asyncio.run(llm.create("M2", "understand", messages=[]))
    assert be.calls[-1][0] == G
    sw = [e for e in sink.events if e.type == "llm.model_switched"]
    assert sw and sw[0].data["to_model"] == Q and sw[0].data["permanent"] is False


def test_invalid_credentials_drop_that_provider_for_the_run():
    llm, be, _ = make_llm([G, Q], {G: [ModelUnavailable("Gemini rejected the API key (401)")], Q: [ok(Q), ok(Q)]})
    asyncio.run(llm.create("M1", "x", messages=[]))
    asyncio.run(llm.create("M2", "x", messages=[]))
    assert llm.models == [Q] and [c[0] for c in be.calls] == [G, Q, Q]


def test_all_providers_failing_transiently_fails_the_call_but_not_the_run():
    llm, _, _ = make_llm([G, Q], {G: [ModelUnavailable("busy", permanent=False)],
                                  Q: [ModelUnavailable("busy", permanent=False)]})
    with pytest.raises(LLMError) as e:
        asyncio.run(llm.create("M1", "x", messages=[]))
    assert not isinstance(e.value, LLMStopped) and llm.stopped is None


def test_all_providers_exhausted_stops_the_run_with_an_alert():
    llm, _, sink = make_llm([G, Q], {G: [ModelUnavailable("daily quota")], Q: [ModelUnavailable("daily limit")]})
    with pytest.raises(LLMStopped):
        asyncio.run(llm.create("M1", "x", messages=[]))
    assert llm.stopped and [e for e in sink.events if e.type == "llm.stopped"]


def test_deadline_caps_the_per_call_timeout_and_stops_further_attempts():
    llm, be, sink = make_llm([G, Q], {G: [("sleep", 0.6)], Q: [ok(Q)]}, call_timeout_s=8.0)

    async def go():
        with message_deadline(1.0):
            return await llm.create("M1", "act", messages=[])
    with pytest.raises(DeadlineExceeded):
        asyncio.run(go())
    assert be.calls[0][1] <= 1.0                                  # per-call timeout shrunk to the deadline
    assert [e for e in sink.events if e.type == "llm.deadline"]


def test_own_pacing_queue_does_not_consume_the_message_deadline():
    llm, be, _ = make_llm([G], {G: [ok(G), ok(G), ok(G)]}, max_rpm=60.0)   # 1 s between calls

    async def go():
        with message_deadline(1.5):                               # ~2 s of queueing would exceed it if counted
            for p in ("a", "b", "c"):
                await llm.create("M1", p, messages=[])
    asyncio.run(go())
    assert len(be.calls) == 3


def test_malformed_understanding_degrades_to_rules_with_clinical_routed_urgently(settings, data):
    """Two invalid JSON answers: the message is processed without AI, marked degraded, and a clinical message is
    not treated as routine."""
    bad = Response(Content(), "end_turn", {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                                           "cache_creation_input_tokens": 0}, G)
    from frontdesk.llm import Block
    bad.content.append(Block("text", text="{not json"))

    class Fake:
        settings = Settings(mode="llm")

        async def create(self, *a, **k):
            return bad
        text_of = staticmethod(LLM.text_of)

    msg = Message(id="X1", received_at=datetime.fromisoformat("2026-11-09T09:00:00-06:00"), channel="sms",
                  from_contact="555-0102", body="I have a rash on my arm since yesterday")

    async def go():
        async with ToolClient(settings, EventBus(), data) as tc:
            return await Pipeline(Settings(mode="llm"), EventBus(), tc, data, Fake()).process(msg)
    mr = asyncio.run(go())
    assert mr.trace["degraded"]
    assert "clinician_urgent" in mr.result["routed_to"]


def test_red_flag_deferred_to_ai_escalates_when_ai_fails(settings, data):
    class Down:
        settings = Settings(mode="llm")

        async def create(self, *a, **k):
            raise LLMStopped("all models exhausted")
        text_of = staticmethod(LLM.text_of)

    msg = Message(id="X2", received_at=datetime.fromisoformat("2026-11-09T09:00:00-06:00"), channel="sms",
                  from_contact="555-0102", body="No chest pain, but I feel strange, can I be seen?")

    async def go():
        async with ToolClient(settings, EventBus(), data) as tc:
            return await Pipeline(Settings(mode="llm"), EventBus(), tc, data, Down()).process(msg)
    out = asyncio.run(go()).result
    assert out["actions"] == ["escalate_emergency"]


# ------------------------------------------------------------------ Groq translation
def test_groq_request_translation_for_tools_and_tool_results(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    g = GroqBackend()
    prior = Content([type("B", (), {"type": "tool_use", "id": "c1", "name": "get_clinic_info", "input": {"topic": "hours"},
                                    "text": None})()])
    prior.provider = "gemini"                                     # a turn produced by another provider
    body = g._body("qwen/qwen3.8-27b", {
        "system": [{"type": "text", "text": "sys"}], "max_tokens": 100,
        "tools": [{"name": "get_clinic_info", "description": "d", "input_schema": {"type": "object"}, "strict": True}],
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": prior},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                   "content": '{"ok": true}'}]}]})
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "get_clinic_info",
                                                                "arguments": json.dumps({"topic": "hours"})}
    assert body["messages"][3]["tool_call_id"] == "c1" and "response_format" not in body
    assert body["reasoning_format"] == "hidden"


def test_groq_structured_output_request_and_tool_call_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    g = GroqBackend()
    body = g._body("openai/gpt-oss-120b", {"messages": [{"role": "user", "content": "x"}],
                                           "output_config": {"effort": "low", "format": {
                                               "type": "json_schema", "schema": {"type": "object"}}}})
    assert body["response_format"]["type"] == "json_schema" and body["include_reasoning"] is False
    r = g._response("groq:openai/gpt-oss-120b", {
        "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "book_appointment", "arguments": '{"slot_id": "S1"}'}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    assert r.stop_reason == "tool_use" and r.content[0].input == {"slot_id": "S1"} and r.usage["input_tokens"] == 100


@pytest.mark.parametrize("status,payload,permanent", [
    (401, {"error": {"message": "Invalid API Key"}}, True),
    (429, {"error": {"message": "Rate limit reached for model on requests per day (RPD): Limit 1000"}}, True),
    (429, {"error": {"message": "Rate limit reached on tokens per minute (TPM): Limit 8000"}}, False),
])
def test_groq_error_classification(monkeypatch, status, payload, permanent):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    g = GroqBackend()

    def handler(request):
        return httpx.Response(status, json=payload, headers={"retry-after": "30"})
    g.client = httpx.AsyncClient(base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler))

    async def go():
        with message_deadline(5.0):                               # a 30 s retry-after does not fit
            return await g.create(timeout_s=5, model="groq:qwen/qwen3.8-27b", messages=[{"role": "user",
                                                                                          "content": "x"}])
    with pytest.raises(ModelUnavailable) as e:
        asyncio.run(go())
    assert e.value.permanent is permanent


def test_groq_network_error_is_retried_once_then_reported(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    g = GroqBackend()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")
    g.client = httpx.AsyncClient(base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler))
    real_sleep = asyncio.sleep
    monkeypatch.setattr("frontdesk.llm.asyncio.sleep", lambda s: real_sleep(0))
    t0 = time.monotonic()
    with pytest.raises(ModelUnavailable) as e:
        asyncio.run(g.create(timeout_s=5, model="groq:qwen/qwen3.8-27b", messages=[{"role": "user", "content": "x"}]))
    assert calls["n"] == 2 and e.value.permanent is False and time.monotonic() - t0 < 5


def test_slow_model_cools_down_so_later_calls_go_straight_to_the_backup():
    llm, be, _ = make_llm([G, Q], {G: [ModelUnavailable("timed out", permanent=False), ok(G)],
                                   Q: [ok(Q), ok(Q)]})
    asyncio.run(llm.create("M1", "a", messages=[]))
    asyncio.run(llm.create("M1", "b", messages=[]))           # Gemini is cooling down: not tried first
    assert [c[0] for c in be.calls] == [G, Q, Q]
    llm._cooling[G] = 0                                       # cool-down over: Gemini is first again
    asyncio.run(llm.create("M2", "c", messages=[]))
    assert be.calls[-1][0] == G
