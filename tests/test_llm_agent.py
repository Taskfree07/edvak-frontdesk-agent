"""The LLM agent loop and understander, driven by a scripted fake model (no network, no API key).

Checks the parts we own: tool calls go through MCP with the context injected, orchestrator-only tools are refused,
submit_resolution ends the loop, and the full pipeline uses LLM output correctly."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from typing import Any

from frontdesk.act.llm_agent import LLMAgent, scoped_tools
from frontdesk.config import Settings
from frontdesk.data import load_messages
from frontdesk.events import EventBus, ListSink
from frontdesk.llm import LLM
from frontdesk.pipeline import Pipeline
from frontdesk.tools.client import Ledger, ToolClient
from frontdesk.understand.llm import LLMUnderstander
from frontdesk.understand.schema import Understanding


def tool_use(i: int, name: str, inp: dict[str, Any]) -> Any:
    return NS(type="tool_use", id=f"tu{i}", name=name, input=inp)


def resp(*blocks: Any, stop: str = "tool_use") -> Any:
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5))


class FakeLLM:
    def __init__(self, script: list[Any]):
        self.settings = Settings(mode="llm")
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, message_id, purpose, **kwargs):
        self.calls.append({"purpose": purpose, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kwargs) if callable(item) else item

    text_of = staticmethod(LLM.text_of)


def test_scoped_tools_hide_phi_tools_from_unverified():
    assert "get_medications" in scoped_tools(["refill"], verified=True)
    assert "get_medications" not in scoped_tools(["refill"], verified=False)
    assert "book_appointment" not in scoped_tools(["schedule", "billing"], verified=False)


def test_agent_loop_uses_mcp_and_refuses_orchestrator_tools(settings, data):
    script = [
        resp(tool_use(1, "check_refill_eligibility", {"medication": "lisinopril"}),
             tool_use(2, "export_state", {})),
        resp(tool_use(3, "prepare_refill_request", {"medication": "lisinopril", "note": ""})),
        resp(tool_use(4, "submit_resolution", {"outcomes": [{"intent": "refill", "outcome": "refill_prepared",
                                                             "detail": "Lisinopril"}],
                                               "offered_slot_ids": [], "note_for_staff": ""})),
    ]
    fake = FakeLLM(script)

    async def go():
        async with ToolClient(settings, EventBus(), data) as tc:
            led = Ledger("M001")
            r = await tc.call(led, "find_patient", {"message_id": "M001", "body": "refill lisinopril",
                                                    "from_contact": "555-0102",
                                                    "received_at": "2026-11-09T07:02:00-06:00"}, by="orchestrator")
            led.context_id = r["context_id"]
            msg = load_messages(settings.messages_path)[0]
            from frontdesk.policy.identity import identify
            ident = identify(data.patients, msg.from_contact, msg.from_name, None, msg.body)
            res = await LLMAgent(fake, tc, data.clinic).act(msg, ident, Understanding(intents=["refill"]), led, r,
                                                            False)
            return res, led, tc.service
    res, led, svc = asyncio.run(go())
    assert res.outcomes[0].outcome == "refill_prepared"
    assert [c.name for c in led.calls] == ["find_patient", "check_refill_eligibility", "export_state",
                                           "prepare_refill_request"]
    assert led.calls[2].result["reason"] == "not_permitted"
    assert len(svc.state.refill_requests) == 1
    tools_offered = {t["name"] for t in fake.calls[0]["tools"]}
    assert "export_state" not in tools_offered and "submit_resolution" in tools_offered
    # the injected context id never appears in what the model sees
    assert all("context_id" not in json.dumps(t["input_schema"]) for t in fake.calls[0]["tools"])


def test_understander_parses_structured_output(data):
    payload = Understanding(intents=["schedule"], visit_type="annual_physical", language="es").model_dump()
    fake = FakeLLM([NS(content=[NS(type="text", text=json.dumps(payload))], stop_reason="end_turn",
                       usage=NS(input_tokens=1, output_tokens=1))])
    msg = load_messages(Settings().messages_path)[15]
    from frontdesk.policy.identity import identify
    ident = identify(data.patients, msg.from_contact, msg.from_name, None, msg.body)
    und = asyncio.run(LLMUnderstander(fake, data.clinic).understand(msg, ident))
    assert und.visit_type == "annual_physical" and und.language == "es"
    sent = fake.calls[0]
    assert "<message" in sent["messages"][0]["content"]          # body passed as quoted data
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_ai_failure_after_booking_is_not_redone_by_fallback(settings, data):
    """The AI books, then the model stops. The rules fallback must not book again; a human finishes the job."""
    from frontdesk.llm import LLMStopped
    und = Understanding(intents=["schedule"], provider="PATEL", visit_type="follow_up", urgency="routine")

    def book_first_slot(kwargs):
        slots = json.loads(kwargs["messages"][-1]["content"][0]["content"])["slots"]
        return resp(tool_use(2, "book_appointment", {"slot_id": slots[0]["slot_id"]}))
    fake = FakeLLM([
        NS(content=[NS(type="text", text=und.model_dump_json())], stop_reason="end_turn",
           usage=NS(input_tokens=1, output_tokens=1)),
        resp(tool_use(1, "find_open_slots", {"date_from": "2026-11-17", "date_to": "2026-11-17",
                                             "visit_type": "follow_up", "provider": "PATEL",
                                             "part_of_day": "morning"})),
        book_first_slot,
        LLMStopped("Gemini daily quota exhausted"),
    ])
    msg = load_messages(settings.messages_path)[9]                  # M010: follow-up with Dr. Patel
    assert msg.id == "M010"

    async def go():
        async with ToolClient(settings, EventBus(), data) as tc:
            pipe = Pipeline(Settings(mode="llm"), EventBus(), tc, data, fake)  # type: ignore[arg-type]
            mr = await pipe.process(msg)
            return mr, tc.service
    mr, svc = asyncio.run(go())
    bookings = [a for a in svc.state.appointments if a.patient_id == "P013" and a.date.isoformat() == "2026-11-17"]
    assert len(bookings) == 1                                        # booked once, not again by the fallback
    assert mr.trace["degraded"] and mr.trace["ai_actions_before_fallback"] == ["book_appointment"]
    assert "book_appointment" in mr.result["actions"] and "route_to_front_desk" in mr.result["actions"]
    assert [c for c in mr.result["tool_calls"] if c == "book_appointment"] == ["book_appointment"]


def test_llm_concern_escalates_message_keywords_missed(settings, data):
    """A message with no red-flag keywords is still escalated when the LLM judges it an emergency."""
    und = Understanding(intents=["clinical"], urgency="emergency", clinical_concern="emergency")
    fake = FakeLLM([NS(content=[NS(type="text", text=und.model_dump_json())], stop_reason="end_turn",
                       usage=NS(input_tokens=1, output_tokens=1))])
    from frontdesk.data import Message
    from datetime import datetime
    msg = Message(id="X1", received_at=datetime.fromisoformat("2026-11-09T09:00:00-06:00"),
                  from_contact="555-0103", body="I feel really strange, my heart is racing and everything is spinning")

    async def go():
        bus = EventBus()
        sink = ListSink()
        bus.subscribe(sink)
        async with ToolClient(settings, bus, data) as tc:
            pipe = Pipeline(Settings(mode="llm"), bus, tc, data, fake)  # type: ignore[arg-type]
            return await pipe.process(msg)
    out = asyncio.run(go()).result
    assert out["actions"] == ["escalate_emergency"] and out["urgency"] == "emergency"
    assert "call 911" in out["reply_draft"]
