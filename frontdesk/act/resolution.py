"""What the acting agent claims it did. Checked against the tool ledger before anything is trusted."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Outcome = Literal[
    "refill_prepared", "refill_too_early", "refill_needs_visit", "refill_to_provider", "booked", "rescheduled",
    "cancelled", "offered_alternatives", "offered_appointment", "offered_new_patient_booking", "answered",
    "routed_billing", "routed_front_desk", "routed_clinician", "verification_requested", "disclosure_declined",
    "acknowledged", "no_action", "could_not_complete",
]
OUTCOMES: list[str] = list(Outcome.__args__)  # type: ignore[attr-defined]


class OutcomeItem(BaseModel):
    intent: str
    outcome: Outcome
    detail: str = ""


class Resolution(BaseModel):
    outcomes: list[OutcomeItem] = Field(default_factory=list)
    offered_slot_ids: list[str] = Field(default_factory=list)
    note_for_staff: str = ""


SUBMIT_TOOL = {
    "name": "submit_resolution",
    "description": "Finish: report what you did for EACH request in the message. Call exactly once, last. "
                   "offered_slot_ids must be slot_ids returned by find_open_slots/check_slot that you want to "
                   "offer the patient (alternatives, or appointment options for a refill that needs a visit).",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["outcomes", "offered_slot_ids", "note_for_staff"],
        "properties": {
            "outcomes": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["intent", "outcome", "detail"],
                    "properties": {
                        "intent": {"type": "string"},
                        "outcome": {"type": "string", "enum": OUTCOMES},
                        "detail": {"type": "string"},
                    },
                },
            },
            "offered_slot_ids": {"type": "array", "items": {"type": "string"}},
            "note_for_staff": {"type": "string"},
        },
    },
}
