"""Typed clinic data loaded from the JSON files, plus the mutable per-run state."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Caregiver(BaseModel):
    name: str
    relationship: str
    phone: str


class Patient(BaseModel):
    id: str
    name: str
    dob: date
    phone: str
    email: str
    primary_provider: str
    last_visit: date
    allergies: list[str] = []
    preferred_language: str = "English"
    authorised_caregiver: Caregiver | None = None

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


class Medication(BaseModel):
    patient_id: str
    medication: str
    controlled_schedule: str | None = None
    days_supply: int
    last_filled: date
    refills_remaining: int
    prescriber: str


class Appointment(BaseModel):
    id: str
    patient_id: str
    provider: str
    date: date
    start: str
    end: str
    visit_type: str
    status: str = "booked"

    @property
    def start_t(self) -> time:
        return time.fromisoformat(self.start)

    @property
    def end_t(self) -> time:
        return time.fromisoformat(self.end)


class Provider(BaseModel):
    id: str
    name: str
    role: str
    languages: list[str] = []
    leave: list[tuple[date, date]] = []

    def on_leave(self, d: date) -> bool:
        return any(a <= d <= b for a, b in self.leave)


class Hours(BaseModel):
    days: list[str]
    open: str
    close: str
    lunch: tuple[str, str]


class Clinic(BaseModel):
    clinic_name: str
    today: date
    timezone: str
    hours: Hours
    holidays: list[date]
    providers: list[Provider]
    visit_types: dict[str, int]
    booking_rules: list[str]
    refill_policy: dict[str, Any]
    safety_policy: str
    privacy_policy: str
    routing_queues: list[str]

    def provider(self, pid: str) -> Provider | None:
        return next((p for p in self.providers if p.id == pid), None)


class Message(BaseModel):
    id: str
    received_at: datetime
    channel: str = "sms"
    from_name: str | None = None
    from_contact: str | None = None
    body: str
    stated_dob: date | None = None


class Label(BaseModel):
    id: str
    patient_id: str | None
    intents: list[str]
    accepted_urgency: list[str]
    expected_actions: list[str]
    must_not: list[str] = []
    trap: str = ""


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class ClinicData(BaseModel):
    """Reference data. Appointments here are the *initial* schedule; tools mutate ClinicState."""

    clinic: Clinic
    patients: list[Patient]
    medications: list[Medication]
    appointments: list[Appointment]

    @classmethod
    def load(cls, data_dir: Path) -> "ClinicData":
        return cls(
            clinic=Clinic.model_validate(_load(data_dir / "clinic.json")),
            patients=[Patient.model_validate(p) for p in _load(data_dir / "patients.json")],
            medications=[Medication.model_validate(m) for m in _load(data_dir / "medications.json")],
            appointments=[Appointment.model_validate(a) for a in _load(data_dir / "appointments.json")],
        )

    def patient(self, pid: str | None) -> Patient | None:
        return next((p for p in self.patients if p.id == pid), None)

    def meds_for(self, pid: str) -> list[Medication]:
        return [m for m in self.medications if m.patient_id == pid]


class QueueItem(BaseModel):
    queue: str
    message_id: str | None
    patient_id: str | None
    reason: str
    priority: str = "normal"


class RefillRequest(BaseModel):
    message_id: str | None
    patient_id: str
    medication: str
    prescriber: str
    status: str = "awaiting_provider_approval"
    note: str = ""


class OutboxItem(BaseModel):
    message_id: str | None
    patient_id: str | None
    language: str
    text: str
    status: str = "draft_not_sent"


class ClinicState(BaseModel):
    """Everything a run can change. Created fresh from ClinicData at the start of every run."""

    appointments: list[Appointment]
    queue_items: list[QueueItem] = Field(default_factory=list)
    refill_requests: list[RefillRequest] = Field(default_factory=list)
    outbox: list[OutboxItem] = Field(default_factory=list)
    next_appt_seq: int = 1

    @classmethod
    def fresh(cls, data: ClinicData) -> "ClinicState":
        return cls(appointments=copy.deepcopy(data.appointments))

    def new_appointment_id(self) -> str:
        aid = f"N{self.next_appt_seq:03d}"
        self.next_appt_seq += 1
        return aid

    def active_appointments(self) -> list[Appointment]:
        return [a for a in self.appointments if a.status == "booked"]


def load_messages(path: Path) -> list[Message]:
    return [Message.model_validate(m) for m in _load(path)]


def load_labels(path: Path) -> dict[str, Label]:
    return {l["id"]: Label.model_validate(l) for l in _load(path)}
