"""Provider layer: Gemini request/response translation, and the quota/budget stops (no network)."""

from __future__ import annotations

import asyncio

import pytest
from google.genai import errors, types

from frontdesk.config import Settings
from frontdesk.events import EventBus, ListSink
from frontdesk.llm import LLM, Content, GeminiBackend, LLMStopped, ModelUnavailable, Response


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return GeminiBackend()


def test_provider_follows_model_name():
    assert Settings(model="gemini-2.5-flash").provider == "gemini"
    assert Settings(model="claude-opus-5-5").provider == "anthropic"


def test_gemini_response_maps_tool_calls_text_and_usage(gemini):
    raw = types.GenerateContentResponse(
        candidates=[types.Candidate(
            finish_reason=types.FinishReason.STOP,
            content=types.Content(role="model", parts=[
                types.Part(text="thinking...", thought=True),
                types.Part(text="Checking."),
                types.Part(function_call=types.FunctionCall(id="c1", name="check_refill_eligibility",
                                                            args={"medication": "lisinopril"})),
            ]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1000, cached_content_token_count=400, candidates_token_count=50,
            thoughts_token_count=30),
        model_version="gemini-2.5-flash")
    r = gemini._response("gemini-2.5-flash", raw)
    assert r.stop_reason == "tool_use"
    assert [b.type for b in r.content] == ["text", "tool_use"]          # thought parts are not answer text
    assert r.content[1].name == "check_refill_eligibility" and r.content[1].input == {"medication": "lisinopril"}
    assert r.usage == {"input_tokens": 600, "output_tokens": 80, "cache_read_input_tokens": 400,
                       "cache_creation_input_tokens": 0}
    assert r.content.raw is raw.candidates[0].content


def test_gemini_safety_block_is_a_refusal(gemini):
    raw = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason=types.FinishReason.SAFETY)])
    assert gemini._response("gemini-2.5-flash", raw).stop_reason == "refusal"


def test_gemini_replays_model_turn_and_names_tool_results(gemini):
    model_turn = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
        id="c1", name="get_clinic_info", args={}))])
    prior = Content([type("B", (), {"type": "tool_use", "id": "c1", "name": "get_clinic_info"})()])
    prior.raw = model_turn
    prior.provider = "gemini"
    contents = gemini._contents([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": prior},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"ok": true}'}]},
    ])
    assert contents[1] is model_turn                                   # replayed verbatim (thought signatures)
    fr = contents[2].parts[0].function_response
    assert fr.name == "get_clinic_info" and fr.id == "c1" and fr.response == {"result": {"ok": True}}


def test_gemini_structured_output_and_thinking_config(gemini):
    cfg = gemini._config("gemini-2.5-flash", {
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}], "max_tokens": 100,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}})
    assert cfg.system_instruction == "sys" and cfg.response_mime_type == "application/json"
    assert cfg.thinking_config.thinking_budget == 512


class _Backend:
    def __init__(self, script):
        self.script = list(script)

    async def create(self, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(inp=1000, out=100):
    return Response(Content(), "end_turn", {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0,
                                            "cache_creation_input_tokens": 0}, "gemini-2.5-flash")


def _llm(script, fallbacks=(), **kw):
    bus = EventBus()
    sink = ListSink()
    bus.subscribe(sink)
    llm = LLM.__new__(LLM)
    llm.settings = Settings(model="gemini-2.5-flash", **kw)
    llm.bus = bus
    llm.backend = _Backend(script)
    llm.totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "cost_usd": 0.0, "calls": 0}
    llm.stopped = None
    llm.models = ["gemini-2.5-flash", *fallbacks]
    llm.switches = []
    llm._min_interval = 0.0
    llm._next_slot = 0.0
    llm._lock = asyncio.Lock()
    return llm, sink


def test_quota_exhaustion_alerts_once_and_blocks_later_calls():
    llm, sink = _llm([_resp(), ModelUnavailable("Gemini daily quota exhausted for gemini-2.5-flash")], max_cost_usd=None)
    asyncio.run(llm.create("M1", "understand", messages=[]))
    with pytest.raises(LLMStopped):
        asyncio.run(llm.create("M2", "understand", messages=[]))
    with pytest.raises(LLMStopped):                                    # no backend call is attempted any more
        asyncio.run(llm.create("M3", "understand", messages=[]))
    stops = [e for e in sink.events if e.type == "llm.stopped"]
    assert len(stops) == 1 and "quota" in stops[0].data["reason"]
    assert llm.totals["calls"] == 1


def test_exhausted_model_switches_to_next_in_chain_then_stops_when_chain_is_empty():
    llm, sink = _llm([ModelUnavailable("daily quota exhausted for gemini-2.5-flash"), _resp(),
                      ModelUnavailable("daily quota exhausted for gemini-3.5-flash-lite")],
                     fallbacks=["gemini-3.5-flash-lite"], max_cost_usd=None)
    asyncio.run(llm.create("M1", "understand", messages=[]))           # retried on the next model
    assert llm.models == ["gemini-3.5-flash-lite"] and llm.stopped is None
    assert [e.data["to_model"] for e in sink.events if e.type == "llm.model_switched"] == ["gemini-3.5-flash-lite"]
    with pytest.raises(LLMStopped):
        asyncio.run(llm.create("M2", "understand", messages=[]))
    assert llm.stopped and "gemini-3.5-flash-lite" in llm.stopped


def test_fallback_chain_never_crosses_providers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    s = Settings(model="claude-opus-5-5", model_fallbacks=["gemini-3.5-flash-lite", "claude-sonnet-5-5"])
    llm = LLM(s, EventBus())
    assert llm.models == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_cost_budget_stops_the_run():
    # 1M input tokens at $0.30/M = $0.30 per call; budget $0.50 is crossed on the second call
    llm, sink = _llm([_resp(1_000_000, 0), _resp(1_000_000, 0), _resp()], max_cost_usd=0.5)
    asyncio.run(llm.create("M1", "x", messages=[]))
    assert llm.stopped is None
    asyncio.run(llm.create("M2", "x", messages=[]))
    assert llm.stopped and "budget" in llm.stopped
    with pytest.raises(LLMStopped):
        asyncio.run(llm.create("M3", "x", messages=[]))


def test_per_minute_429_waits_and_retries(gemini, monkeypatch):
    calls = {"n": 0}
    ok = types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason=types.FinishReason.STOP, content=types.Content(role="model", parts=[types.Part(text="hi")]))])

    async def flaky(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                     "message": "You exceeded your current quota, please check your "
                                                                "plan and billing details.",
                                                     "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "5"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"}]}})
        return ok

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
    gemini.max_wait = 10.0
    monkeypatch.setattr(gemini.client.aio.models, "generate_content", flaky)
    monkeypatch.setattr("frontdesk.llm.asyncio.sleep", fake_sleep)
    r = asyncio.run(gemini.create(timeout_s=8, model="gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}]))
    assert calls["n"] == 2 and slept == [6.0] and r.content[0].text == "hi"


def test_daily_quota_429_is_not_retried(gemini, monkeypatch):
    calls = {"n": 0}

    async def boom(**_):
        calls["n"] += 1
        raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
                                                 "Quota exceeded for metric generate_content_free_tier_requests, "
                                                 "limit: 250, quotaId: GenerateRequestsPerDayPerProjectPerModel"}})
    monkeypatch.setattr(gemini.client.aio.models, "generate_content", boom)
    with pytest.raises(LLMStopped):
        asyncio.run(gemini.create(timeout_s=8, model="gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}]))
    assert calls["n"] == 1
