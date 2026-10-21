"""ClinicService: every clinic tool as a plain Python method. Policy is enforced HERE, not in prompts.

Conventions
- Every method returns a JSON-serialisable dict with "ok": bool. Business refusals are results, not exceptions,
  so the agent can read the reason and adapt.
- PHI tools take a `context_id` opened by `find_patient`. The context fixes WHO the message is about; the LLM can
  never choose a patient id, and an unverified context gets no record data.
- There is deliberately no tool to approve a refill or to change contact details.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from frontdesk.data import Appointment, ClinicData, ClinicState, OutboxItem, QueueItem, RefillRequest
from frontdesk.policy.calendar import local_now
from frontdesk.policy.identity import Identity, identify
from frontdesk.policy.medications import match_medication
from frontdesk.policy.refill import check_refill
from frontdesk.policy.scheduling import Slot, find_open_slots, validate_slot

QUEUES = ("clinician_urgent", "clinician_routine", "provider_refills", "front_desk", "billing")
MAX_BOOKINGS_PER_MESSAGE = 3   # guard against a looping agent (a cancel-the-day request can touch several)
MAX_SEARCH_DAYS = 31


@dataclass
class MessageContext:
    context_id: str
    message_id: str
    identity: Identity
    now: tuple[date, int]
    bookings: int = 0


class ClinicService:
    def __init__(self, data: ClinicData):
        self.data = data
        self.state = ClinicState.fresh(data)
        self.contexts: dict[str, MessageContext] = {}
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ helpers
    @property
    def today(self) -> date:
        return self.data.clinic.today

    def _ctx(self, context_id: str) -> MessageContext | None:
        return self.contexts.get(context_id)

    def _require_verified(self, context_id: str) -> tuple[MessageContext | None, dict | None]:
        ctx = self._ctx(context_id)
        if ctx is None:
            return None, {"ok": False, "reason": "unknown_context", "message": "Unknown context_id."}
        if not ctx.identity.verified or not ctx.identity.patient_id:
            return None, {"ok": False, "reason": "sender_not_verified",
                          "message": "Sender is not verified for this patient; no record data can be used. "
                                     "Ask the sender to verify (name + date of birth, or contact on file)."}
        return ctx, None

    def _appt_view(self, a: Appointment) -> dict[str, Any]:
        prov = self.data.clinic.provider(a.provider)
        return {"appointment_id": a.id, "provider": a.provider, "provider_name": prov.name if prov else a.provider,
                "date": a.date.isoformat(), "weekday": a.date.strftime("%A"), "start": a.start, "end": a.end,
                "visit_type": a.visit_type, "status": a.status}

    def _slot_view(self, s: Slot) -> dict[str, Any]:
        prov = self.data.clinic.provider(s.provider)
        return {"slot_id": s.slot_id, "provider": s.provider, "provider_name": prov.name if prov else s.provider,
                "date": s.date.isoformat(), "weekday": s.date.strftime("%A"), "start": s.start, "end": s.end,
                "visit_type": s.visit_type}

    # ------------------------------------------------------------------ identity
    def find_patient(self, message_id: str, from_contact: str | None, from_name: str | None,
                     stated_dob: str | None, body: str, received_at: str) -> dict[str, Any]:
        """Verify the sender and open the message context (orchestrator-only)."""
        dob = date.fromisoformat(stated_dob) if stated_dob else None
        ident = identify(self.data.patients, from_contact, from_name, dob, body)
        cid = f"ctx-{message_id}-{next(self._ids)}"
        self.contexts[cid] = MessageContext(cid, message_id, ident, local_now(datetime.fromisoformat(received_at)))
        out: dict[str, Any] = {"ok": True, "context_id": cid, **ident.model_dump()}
        p = self.data.patient(ident.patient_id)
        if p and ident.verified:
            out.update(primary_provider=p.primary_provider, preferred_language=p.preferred_language,
                       age=(self.today - p.dob).days // 365)
        elif p:
            out.update(preferred_language=p.preferred_language)
        return out

    # ------------------------------------------------------------------ clinic facts
    def get_clinic_info(self, topic: str = "all") -> dict[str, Any]:
        c = self.data.clinic
        facts: dict[str, Any] = {
            "hours": {"days": c.hours.days, "open": c.hours.open, "close": c.hours.close,
                      "lunch_closed": list(c.hours.lunch), "timezone": c.timezone},
            "holidays": [{"date": d.isoformat(), "weekday": d.strftime("%A")} for d in c.holidays],
            "providers": [{"id": p.id, "name": p.name, "role": p.role, "languages": p.languages,
                           "leave": [[a.isoformat(), b.isoformat()] for a, b in p.leave]} for p in c.providers],
            "visit_types_minutes": c.visit_types,
            "new_patients": {"accepting": "new_patient" in c.visit_types,
                             "visit_type": "new_patient", "minutes": c.visit_types.get("new_patient"),
                             "how_to_book": "The front desk books new-patient visits and collects registration "
                                            "details."},
            "today": self.today.isoformat(),
        }
        if topic in facts:
            return {"ok": True, "topic": topic, topic: facts[topic]}
        return {"ok": True, "topic": "all", **facts}

    # ------------------------------------------------------------------ medications / refills
    def get_medications(self, context_id: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        meds = self.data.meds_for(ctx.identity.patient_id)
        return {"ok": True, "medications": [
            {"medication": m.medication, "controlled": bool(m.controlled_schedule), "prescriber": m.prescriber}
            for m in meds]}

    def check_refill_eligibility(self, context_id: str, medication: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        pid = ctx.identity.patient_id
        med = match_medication(medication, self.data.meds_for(pid))
        decision = check_refill(self.data.patient(pid), med, medication, self.today)
        return {"ok": True, **decision.model_dump(mode="json")}

    def prepare_refill_request(self, context_id: str, medication: str, note: str = "") -> dict[str, Any]:
        """Queue a refill request for provider approval. Only possible when eligibility is `eligible`."""
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        pid = ctx.identity.patient_id
        med = match_medication(medication, self.data.meds_for(pid))
        decision = check_refill(self.data.patient(pid), med, medication, self.today)
        if not decision.eligible:
            return {"ok": False, "reason": f"not_eligible:{decision.status}", "eligibility": decision.status,
                    "message": decision.reason}
        existing = next((r for r in self.state.refill_requests
                         if r.message_id == ctx.message_id and r.medication == med.medication), None)
        if existing is None:
            self.state.refill_requests.append(RefillRequest(
                message_id=ctx.message_id, patient_id=pid, medication=med.medication, prescriber=med.prescriber,
                note=note[:300]))
            self._queue(ctx, "provider_refills", f"Refill for approval: {med.medication}", "normal")
        return {"ok": True, "medication": med.medication, "prescriber": med.prescriber,
                "status": "awaiting_provider_approval", "queue": "provider_refills",
                "message": "Prepared for provider approval. Not approved or sent - a provider must approve it."}

    # ------------------------------------------------------------------ appointments
    def get_patient_appointments(self, context_id: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        appts = sorted((a for a in self.state.active_appointments()
                        if a.patient_id == ctx.identity.patient_id and a.date >= self.today),
                       key=lambda a: (a.date, a.start))
        return {"ok": True, "appointments": [self._appt_view(a) for a in appts]}

    def find_open_slots(self, context_id: str, date_from: str, date_to: str, visit_type: str,
                        provider: str | None = None, part_of_day: str = "any") -> dict[str, Any]:
        ctx = self._ctx(context_id)
        c = self.data.clinic
        try:
            d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError:
            return {"ok": False, "reason": "bad_date", "message": "Dates must be YYYY-MM-DD."}
        d0 = max(d0, self.today)
        if d1 < d0:
            return {"ok": False, "reason": "bad_range", "message": "date_to is before date_from (or in the past)."}
        d1 = min(d1, d0 + timedelta(days=MAX_SEARCH_DAYS))
        if visit_type not in c.visit_types:
            return {"ok": False, "reason": "bad_visit_type", "message": f"visit_type must be one of {list(c.visit_types)}."}
        if part_of_day not in ("morning", "afternoon", "any"):
            part_of_day = "any"
        if provider:
            provider = provider.upper()
            if c.provider(provider) is None:
                return {"ok": False, "reason": "unknown_provider",
                        "message": f"provider must be one of {[p.id for p in c.providers]}."}
            providers = [provider]
        else:
            primary = None
            if ctx and ctx.identity.verified and ctx.identity.patient_id:
                primary = self.data.patient(ctx.identity.patient_id).primary_provider
            providers = ([primary] if primary else []) + [p.id for p in c.providers if p.id != primary]
        not_before = ctx.now if ctx else None
        slots = find_open_slots(c, self.state.appointments, providers, d0, d1, visit_type, part_of_day, not_before)
        notes = []
        for pid in providers:
            prov = c.provider(pid)
            for a, b in prov.leave:
                if a <= d1 and d0 <= b:
                    notes.append(f"{prov.name} is on leave {a.isoformat()} to {b.isoformat()}.")
        closed = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)
                  if (d0 + timedelta(days=i)) in c.holidays]
        notes += [f"{d.isoformat()} is a clinic holiday." for d in closed]
        return {"ok": True, "searched": {"date_from": d0.isoformat(), "date_to": d1.isoformat(),
                                         "providers": providers, "visit_type": visit_type, "part_of_day": part_of_day},
                "slots": [self._slot_view(s) for s in slots], "notes": notes}

    def check_slot(self, context_id: str, provider: str, day: str, start: str, visit_type: str) -> dict[str, Any]:
        """Is one exact slot bookable? Returns the rules it breaks."""
        ctx = self._ctx(context_id)
        if self.data.clinic.provider(provider.upper()) is None:
            return {"ok": False, "reason": "unknown_provider",
                    "message": f"provider must be one of {[p.id for p in self.data.clinic.providers]}."}
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "reason": "bad_date", "message": "date must be YYYY-MM-DD."}
        problems = validate_slot(self.data.clinic, self.state.appointments, provider.upper(), d, start, visit_type,
                                 ctx.now if ctx else None)
        return {"ok": True, "available": not problems, "problems": problems,
                "slot_id": Slot.make_id(provider.upper(), d, start, visit_type) if not problems else None}

    def _resolve_slot(self, slot_id: str) -> tuple[Slot | None, str | None]:
        try:
            return Slot.parse_id(slot_id, self.data.clinic), None
        except Exception:
            return None, "Invalid slot_id. Use a slot_id returned by find_open_slots."

    def book_appointment(self, context_id: str, slot_id: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        if ctx.bookings >= MAX_BOOKINGS_PER_MESSAGE:
            return {"ok": False, "reason": "limit", "message": "Booking limit for one message reached."}
        slot, perr = self._resolve_slot(slot_id)
        if perr:
            return {"ok": False, "reason": "bad_slot", "message": perr}
        problems = validate_slot(self.data.clinic, self.state.appointments, slot.provider, slot.date, slot.start,
                                 slot.visit_type, ctx.now)
        if problems:
            return {"ok": False, "reason": "slot_unavailable", "problems": problems}
        appt = Appointment(id=self.state.new_appointment_id(), patient_id=ctx.identity.patient_id,
                           provider=slot.provider, date=slot.date, start=slot.start, end=slot.end,
                           visit_type=slot.visit_type, status="booked")
        self.state.appointments.append(appt)
        ctx.bookings += 1
        return {"ok": True, "booked": self._appt_view(appt)}

    def _own_appointment(self, ctx: MessageContext, appointment_id: str) -> tuple[Appointment | None, dict | None]:
        appt = next((a for a in self.state.appointments if a.id == appointment_id), None)
        if appt is None or appt.patient_id != ctx.identity.patient_id:
            return None, {"ok": False, "reason": "not_found",
                          "message": "No such appointment for this patient. Use get_patient_appointments."}
        if appt.status != "booked":
            return None, {"ok": False, "reason": "not_active", "message": f"Appointment is {appt.status}."}
        if appt.date < self.today:
            return None, {"ok": False, "reason": "past", "message": "Appointment is in the past."}
        return appt, None

    def reschedule_appointment(self, context_id: str, appointment_id: str, slot_id: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        appt, aerr = self._own_appointment(ctx, appointment_id)
        if aerr:
            return aerr
        slot, perr = self._resolve_slot(slot_id)
        if perr:
            return {"ok": False, "reason": "bad_slot", "message": perr}
        problems = validate_slot(self.data.clinic, self.state.appointments, slot.provider, slot.date, slot.start,
                                 slot.visit_type, ctx.now, ignore_appointment_id=appt.id)
        if problems:
            return {"ok": False, "reason": "slot_unavailable", "problems": problems}
        before = self._appt_view(appt)
        appt.provider, appt.date, appt.start, appt.end, appt.visit_type = (
            slot.provider, slot.date, slot.start, slot.end, slot.visit_type)
        return {"ok": True, "previous": before, "rescheduled": self._appt_view(appt)}

    def cancel_appointment(self, context_id: str, appointment_id: str) -> dict[str, Any]:
        ctx, err = self._require_verified(context_id)
        if err:
            return err
        appt, aerr = self._own_appointment(ctx, appointment_id)
        if aerr:
            return aerr
        appt.status = "cancelled"
        return {"ok": True, "cancelled": self._appt_view(appt)}

    # ------------------------------------------------------------------ routing / replies
    def _queue(self, ctx: MessageContext | None, queue: str, reason: str, priority: str) -> bool:
        mid = ctx.message_id if ctx else None
        if any(q.queue == queue and q.message_id == mid for q in self.state.queue_items):
            return False
        self.state.queue_items.append(QueueItem(queue=queue, message_id=mid,
                                                patient_id=ctx.identity.patient_id if ctx else None,
                                                reason=reason[:300], priority=priority))
        return True

    def route_to_queue(self, context_id: str, queue: str, reason: str, priority: str = "normal") -> dict[str, Any]:
        if queue not in QUEUES:
            return {"ok": False, "reason": "bad_queue", "message": f"queue must be one of {list(QUEUES)}."}
        ctx = self._ctx(context_id)
        if queue == "clinician_urgent":
            priority = "high"
        created = self._queue(ctx, queue, reason, priority)
        return {"ok": True, "queue": queue, "priority": priority, "created": created}

    def draft_reply(self, context_id: str, text: str, language: str = "en") -> dict[str, Any]:
        """Store a reply draft in the outbox (never sent). Server-side privacy guard for unverified senders."""
        ctx = self._ctx(context_id)
        if ctx and not ctx.identity.verified and ctx.identity.patient_id:
            leaked = self.phi_terms_in(text, ctx.identity.patient_id)
            if leaked:
                return {"ok": False, "reason": "phi_to_unverified_sender", "terms": leaked}
        self.state.outbox.append(OutboxItem(message_id=ctx.message_id if ctx else None,
                                            patient_id=ctx.identity.patient_id if ctx else None,
                                            language=language, text=text))
        return {"ok": True, "status": "draft_saved_not_sent"}

    def phi_terms_in(self, text: str, patient_id: str) -> list[str]:
        """Record details of `patient_id` that appear in `text` (medications, appointment dates, DOB)."""
        p = self.data.patient(patient_id)
        low = text.lower()
        terms: list[str] = []
        for m in self.data.meds_for(patient_id):
            g = re.split(r"[\s/]", m.medication.lower())[0]
            if g and g in low:
                terms.append(m.medication)
        for a in self.state.appointments:
            if a.patient_id == patient_id:
                variants = {a.date.isoformat(), f"{a.date.strftime('%B')} {a.date.day}",
                            f"{a.date.day} {a.date.strftime('%B')}"}
                if any(v.lower() in low for v in variants):
                    terms.append(f"appointment {a.date.isoformat()}")
        if p and p.dob.isoformat() in text:
            terms.append("date of birth")
        return sorted(set(terms))

    # ------------------------------------------------------------------ evaluation / UI
    def export_state(self) -> dict[str, Any]:
        return {"ok": True, **self.state.model_dump(mode="json"),
                "providers": [p.model_dump(mode="json") for p in self.data.clinic.providers],
                "holidays": [d.isoformat() for d in self.data.clinic.holidays]}
