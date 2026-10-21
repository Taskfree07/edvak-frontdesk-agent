"""The Understanding contract shared by the LLM and rules understanders."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["refill", "schedule", "reschedule", "cancel", "clinical", "billing", "records", "admin",
                 "new_patient", "third_party", "spam", "other"]
INTENTS: list[str] = list(Intent.__args__)  # type: ignore[attr-defined]
VISIT_TYPES = ["new_patient", "annual_physical", "follow_up", "sick_visit", "telehealth"]


class TimePreference(BaseModel):
    date_from: str | None = None       # YYYY-MM-DD
    date_to: str | None = None
    part_of_day: Literal["morning", "afternoon", "any"] = "any"
    exact_time: str | None = None      # HH:MM, 24h
    is_exact: bool = False             # patient named one specific provider + day (+ time)


class ExistingAppointment(BaseModel):
    date: str | None = None
    hint: str | None = None            # e.g. "annual physical", "with Dr Patel"
    all_on_date: bool = False          # "cancel my appointment tomorrow, something came up" -> the whole day


class Understanding(BaseModel):
    intents: list[Intent] = Field(default_factory=list)
    summary: str = ""
    medications: list[str] = Field(default_factory=list)
    all_medications: bool = False
    provider: Literal["PATEL", "OKAFOR", "KIM"] | None = None
    visit_type: Literal["new_patient", "annual_physical", "follow_up", "sick_visit", "telehealth"] | None = None
    time_preference: TimePreference = Field(default_factory=TimePreference)
    existing_appointment: ExistingAppointment = Field(default_factory=ExistingAppointment)
    question: str | None = None
    urgency: Literal["emergency", "urgent", "routine"] = "routine"
    clinical_concern: Literal["none", "routine", "urgent_same_day", "emergency"] = "none"
    concern_summary: str | None = None
    suspicious_instruction: bool = False
    suspicious_reason: str | None = None
    language: Literal["en", "es", "other"] = "en"


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


# Hand-written JSON schema for Claude structured output (every property required; optional values are nullable).
UNDERSTANDING_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intents", "summary", "medications", "all_medications", "provider", "visit_type", "time_preference",
                 "existing_appointment", "question", "urgency", "clinical_concern", "concern_summary",
                 "suspicious_instruction", "suspicious_reason", "language"],
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": INTENTS}},
        "summary": {"type": "string"},
        "medications": {"type": "array", "items": {"type": "string"}},
        "all_medications": {"type": "boolean"},
        "provider": _nullable({"type": "string", "enum": ["PATEL", "OKAFOR", "KIM"]}),
        "visit_type": _nullable({"type": "string", "enum": VISIT_TYPES}),
        "time_preference": {
            "type": "object", "additionalProperties": False,
            "required": ["date_from", "date_to", "part_of_day", "exact_time", "is_exact"],
            "properties": {
                "date_from": _nullable({"type": "string"}),
                "date_to": _nullable({"type": "string"}),
                "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
                "exact_time": _nullable({"type": "string"}),
                "is_exact": {"type": "boolean"},
            },
        },
        "existing_appointment": {
            "type": "object", "additionalProperties": False,
            "required": ["date", "hint", "all_on_date"],
            "properties": {
                "date": _nullable({"type": "string"}),
                "hint": _nullable({"type": "string"}),
                "all_on_date": {"type": "boolean"},
            },
        },
        "question": _nullable({"type": "string"}),
        "urgency": {"type": "string", "enum": ["emergency", "urgent", "routine"]},
        "clinical_concern": {"type": "string", "enum": ["none", "routine", "urgent_same_day", "emergency"]},
        "concern_summary": _nullable({"type": "string"}),
        "suspicious_instruction": {"type": "boolean"},
        "suspicious_reason": _nullable({"type": "string"}),
        "language": {"type": "string", "enum": ["en", "es", "other"]},
    },
}
