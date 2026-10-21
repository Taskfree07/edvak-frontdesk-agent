"""The clinic MCP server. Thin wrapper: each tool delegates to ClinicService, where the policy lives.

Run standalone over stdio (any MCP client can connect):
    python -m frontdesk.tools.mcp_server            # or: frontdesk serve-mcp
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from frontdesk.data import ClinicData
from frontdesk.tools.service import ClinicService

# Tools only the orchestrator calls. They are never offered to the LLM.
ORCHESTRATOR_ONLY = {"find_patient", "draft_reply", "export_state"}

Queue = Literal["clinician_urgent", "clinician_routine", "provider_refills", "front_desk", "billing"]
VisitType = Literal["new_patient", "annual_physical", "follow_up", "sick_visit", "telehealth"]
PartOfDay = Literal["morning", "afternoon", "any"]


def build_server(service: ClinicService) -> MCPServer:
    mcp = MCPServer(
        "maplewood-clinic",
        instructions="Maplewood Family Clinic front-desk tools. Clinic policy is enforced inside every tool: "
                     "refills can only be prepared for provider approval, never approved; bookings are validated "
                     "against hours, lunch, holidays, leave and overlaps; patient data needs a verified sender.",
    )

    @mcp.tool(description="Verify the sender of a message and open its context. Returns context_id, whether the "
                          "sender is verified, and the patient the message is about. Orchestrator only.")
    def find_patient(message_id: str, body: str, received_at: str, from_contact: str | None = None,
                     from_name: str | None = None, stated_dob: str | None = None) -> dict[str, Any]:
        return service.find_patient(message_id, from_contact, from_name, stated_dob, body, received_at)

    @mcp.tool(description="Clinic facts: opening hours, holidays, providers (roles, languages, leave), visit types "
                          "and durations, new-patient information. Use it to answer general questions.")
    def get_clinic_info(topic: Literal["hours", "holidays", "providers", "visit_types_minutes", "new_patients",
                                       "all"] = "all") -> dict[str, Any]:
        return service.get_clinic_info(topic)

    @mcp.tool(description="The patient's active medication list (verified senders only).")
    def get_medications(context_id: str) -> dict[str, Any]:
        return service.get_medications(context_id)

    @mcp.tool(description="Check a requested medication against the refill policy. Returns status: eligible | "
                          "controlled | needs_visit | no_refills | too_early (with earliest_request_date) | "
                          "not_on_list. Brand names and Spanish names are understood.")
    def check_refill_eligibility(context_id: str, medication: str) -> dict[str, Any]:
        return service.check_refill_eligibility(context_id, medication)

    @mcp.tool(description="Prepare a refill request and queue it for provider approval. Succeeds ONLY when the "
                          "medication is eligible. This never approves or sends a prescription.")
    def prepare_refill_request(context_id: str, medication: str, note: str = "") -> dict[str, Any]:
        return service.prepare_refill_request(context_id, medication, note)

    @mcp.tool(description="The patient's upcoming booked appointments (verified senders only).")
    def get_patient_appointments(context_id: str) -> dict[str, Any]:
        return service.get_patient_appointments(context_id)

    @mcp.tool(description="Find bookable slots between two dates (YYYY-MM-DD, inclusive), earliest first. Only "
                          "valid slots are returned (hours, lunch, holidays, leave, overlaps, :00/:20/:40 grid, "
                          "visit duration). Omit provider to search the patient's primary provider first, then "
                          "the others. Notes explain leave and holidays in the range.")
    def find_open_slots(context_id: str, date_from: str, date_to: str, visit_type: VisitType,
                        provider: Literal["PATEL", "OKAFOR", "KIM"] | None = None,
                        part_of_day: PartOfDay = "any") -> dict[str, Any]:
        return service.find_open_slots(context_id, date_from, date_to, visit_type, provider, part_of_day)

    @mcp.tool(description="Check whether one exact provider/date/time is bookable; returns the rules it breaks. "
                          "Use it when the patient asks for a specific time.")
    def check_slot(context_id: str, provider: Literal["PATEL", "OKAFOR", "KIM"], day: str, start: str,
                   visit_type: VisitType) -> dict[str, Any]:
        return service.check_slot(context_id, provider, day, start, visit_type)

    @mcp.tool(description="Book a slot for the patient. slot_id must come from find_open_slots or check_slot. "
                          "The slot is re-validated; double-booking, leave and holidays are rejected.")
    def book_appointment(context_id: str, slot_id: str) -> dict[str, Any]:
        return service.book_appointment(context_id, slot_id)

    @mcp.tool(description="Move one of the patient's appointments to a new slot (slot_id from find_open_slots).")
    def reschedule_appointment(context_id: str, appointment_id: str, slot_id: str) -> dict[str, Any]:
        return service.reschedule_appointment(context_id, appointment_id, slot_id)

    @mcp.tool(description="Cancel one of the patient's upcoming appointments.")
    def cancel_appointment(context_id: str, appointment_id: str) -> dict[str, Any]:
        return service.cancel_appointment(context_id, appointment_id)

    @mcp.tool(description="Hand the message to a human queue: clinician_urgent (same-day clinical), "
                          "clinician_routine (clinical questions), provider_refills (refill decisions for the "
                          "prescriber), front_desk (records, admin, contact changes), billing (bills, balances, "
                          "copays, insurance).")
    def route_to_queue(context_id: str, queue: Queue, reason: str,
                       priority: Literal["normal", "high"] = "normal") -> dict[str, Any]:
        return service.route_to_queue(context_id, queue, reason, priority)

    @mcp.tool(description="Save a reply draft to the outbox (never sent). Orchestrator only.")
    def draft_reply(context_id: str, text: str, language: str = "en") -> dict[str, Any]:
        return service.draft_reply(context_id, text, language)

    @mcp.tool(description="Snapshot of the run state (schedule, queues, prepared refills, outbox). Orchestrator only.")
    def export_state() -> dict[str, Any]:
        return service.export_state()

    return mcp


def main() -> None:
    data_dir = Path(os.getenv("FRONTDESK_DATA_DIR", Path(__file__).resolve().parents[2] / "edvak_forge_data"))
    build_server(ClinicService(ClinicData.load(data_dir))).run("stdio")


if __name__ == "__main__":
    main()
