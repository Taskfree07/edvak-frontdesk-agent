"""Tool-level policy enforcement, directly and over MCP (in-process and stdio)."""

from __future__ import annotations

import asyncio

import pytest

from frontdesk.config import Settings
from frontdesk.events import EventBus
from frontdesk.tools.client import Ledger, ToolClient
from frontdesk.tools.mcp_server import ORCHESTRATOR_ONLY
from tests.conftest import open_ctx


def test_phi_tools_refuse_unverified(service):
    ctx = open_ctx(service, "rchen.new@example.org", name="Robert Chen", dob="1971-11-20")
    for call in (lambda: service.get_medications(ctx), lambda: service.get_patient_appointments(ctx),
                 lambda: service.check_refill_eligibility(ctx, "lisinopril"),
                 lambda: service.prepare_refill_request(ctx, "lisinopril")):
        r = call()
        assert r["ok"] is False and r["reason"] == "sender_not_verified"


def test_controlled_refill_can_never_be_prepared(service):
    ctx = open_ctx(service, "555-0106")
    r = service.prepare_refill_request(ctx, "Xanax")
    assert r["ok"] is False and "controlled" in r["reason"]
    assert not service.state.refill_requests


def test_eligible_refill_is_queued_for_approval_only(service):
    ctx = open_ctx(service, "555-0102")
    r = service.prepare_refill_request(ctx, "lisinopril")
    assert r["ok"] and r["status"] == "awaiting_provider_approval"
    assert [q.queue for q in service.state.queue_items] == ["provider_refills"]
    service.prepare_refill_request(ctx, "lisinopril")            # idempotent within a message
    assert len(service.state.refill_requests) == 1


def test_no_approve_tool_exists(service):
    assert not [m for m in dir(service) if "approve" in m]


def test_booking_rejects_double_booking_and_leave(service):
    ctx = open_ctx(service, "555-0102")
    taken = service.book_appointment(ctx, "OKAFOR_20261112_1400_follow_up")
    leave = service.book_appointment(ctx, "OKAFOR_20261118_0900_follow_up")
    assert taken["ok"] is False and taken["reason"] == "slot_unavailable"
    assert leave["ok"] is False and any("leave" in p for p in leave["problems"])


def test_booking_then_slot_is_gone(service):
    ctx = open_ctx(service, "555-0102")
    slot = service.find_open_slots(ctx, "2026-11-13", "2026-11-13", "follow_up", "KIM")["slots"][0]
    assert service.book_appointment(ctx, slot["slot_id"])["ok"]
    again = service.find_open_slots(ctx, "2026-11-13", "2026-11-13", "follow_up", "KIM")["slots"]
    assert slot["slot_id"] not in [s["slot_id"] for s in again]
    ctx2 = open_ctx(service, "555-0103", mid="T2")
    assert service.book_appointment(ctx2, slot["slot_id"])["ok"] is False


def test_cannot_touch_someone_elses_appointment(service):
    ctx = open_ctx(service, "555-0102")          # P002
    other = next(a for a in service.state.appointments if a.patient_id == "P007")
    assert service.cancel_appointment(ctx, other.id)["reason"] == "not_found"


def test_reschedule_moves_and_frees_old_slot(service):
    ctx = open_ctx(service, "priya.raman@example.com")
    appt = next(a for a in service.get_patient_appointments(ctx)["appointments"] if a["date"] == "2026-11-11")
    slot = service.find_open_slots(ctx, "2026-11-13", "2026-11-13", "follow_up", "KIM", "afternoon")["slots"][0]
    r = service.reschedule_appointment(ctx, appt["appointment_id"], slot["slot_id"])
    assert r["ok"] and r["rescheduled"]["date"] == "2026-11-13"


def test_draft_reply_blocks_phi_to_unverified(service):
    ctx = open_ctx(service, "rchen.new@example.org", name="Robert Chen", dob="1971-11-20")
    assert service.draft_reply(ctx, "Your lisinopril is ready")["ok"] is False
    assert service.draft_reply(ctx, "Please verify your identity")["ok"] is True


@pytest.mark.parametrize("transport", ["inproc", "stdio"])
def test_mcp_transport_end_to_end(transport):
    async def go():
        s = Settings(mode="rules", transport=transport)
        async with ToolClient(s, EventBus()) as tc:
            led = Ledger("M1")
            r = await tc.call(led, "find_patient", {"message_id": "M1", "body": "x", "from_contact": "555-0102",
                                                    "received_at": "2026-11-09T08:00:00-06:00"}, by="orchestrator")
            led.context_id = r["context_id"]
            e = await tc.call(led, "check_refill_eligibility", {"medication": "lisinopril"}, by="llm")
            blocked = await tc.call(led, "export_state", {}, by="llm")
            specs = tc.llm_tools(tc.tool_names)
            return e, blocked, specs
    e, blocked, specs = asyncio.run(go())
    assert e["status"] == "eligible"
    assert blocked["reason"] == "not_permitted"
    names = {t["name"] for t in specs}
    assert not names & ORCHESTRATOR_ONLY
    assert all("context_id" not in t["input_schema"]["properties"] for t in specs)
