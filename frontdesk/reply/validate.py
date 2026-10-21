"""Final validator. Runs on every message, after everything else. A failure replaces the reply with a safe
fallback and sends the message to the front desk; the violation is recorded and counted by the eval report."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from frontdesk.data import ClinicData
from frontdesk.outcome import Outcome
from frontdesk.policy.identity import Identity
from frontdesk.reply.compose import Reply, emergency_reply
from frontdesk.reply.templates import fmt_date, fmt_time
from frontdesk.safety import SafetyResult
from frontdesk.tools.client import Ledger

ACTION_VOCAB = {
    "escalate_emergency", "route_to_clinician", "route_to_provider", "route_to_billing", "route_to_front_desk",
    "prepare_refill_for_approval", "refill_needs_visit", "refill_too_early", "book_appointment",
    "reschedule_appointment", "cancel_appointment", "offer_appointment", "offer_alternatives",
    "offer_new_patient_booking", "request_verification", "decline_disclosure", "flag_suspicious_instruction",
    "answer_directly", "acknowledge", "no_action",
}
PHI_READ_TOOLS = {"get_medications", "check_refill_eligibility", "get_patient_appointments"}
CLINICAL_ADVICE = re.compile(
    r"\b(you should (take|stop|increase|decrease)|dose|dosage|\d+\s?mg\b|diagnos\w*|it (likely )?means|normal range"
    r"|symptoms? of|treat(ment)? (is|for)|take (an?|some) \w+)\b", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


@dataclass
class Validation:
    ok: bool
    violations: list[str] = field(default_factory=list)


def _phi_terms(data: ClinicData, ledger: Ledger, patient_id: str, text: str) -> list[str]:
    low = text.lower()
    found = []
    for m in data.meds_for(patient_id):
        g = re.split(r"[\s/]", m.medication.lower())[0]
        if g and re.search(rf"\b{re.escape(g)}\b", low):
            found.append(f"medication:{m.medication}")
    for a in data.appointments:
        if a.patient_id == patient_id and a.date >= data.clinic.today:
            if fmt_date(a.date.isoformat(), "en").lower() in low or a.date.isoformat() in low:
                found.append(f"appointment:{a.date.isoformat()}")
    p = data.patient(patient_id)
    if p and (p.dob.isoformat() in text or fmt_date(p.dob.isoformat(), "en").lower() in low):
        found.append("date_of_birth")
    return found


def _ungrounded(paragraph: str, facts_text: str) -> list[str]:
    """Dates, times, money and phone numbers in an LLM draft that the tool facts do not contain."""
    bad: list[str] = []
    for m in re.finditer(rf"\b({MONTHS})\s+(\d{{1,2}})\b", paragraph):
        mon = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december"].index(m.group(1).lower()) + 1
        iso_frag = f"-{mon:02d}-{int(m.group(2)):02d}"
        if iso_frag not in facts_text:
            bad.append(m.group(0))
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", paragraph, re.I):
        h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
        if f"{h:02d}:{m.group(2)}" not in facts_text:
            bad.append(m.group(0))
    bad += re.findall(r"\$\s?\d[\d,.]*", paragraph)
    bad += [p for p in re.findall(r"\b\d{3}-\d{4}\b", paragraph) if p not in facts_text]
    return bad


def validate(data: ClinicData, identity: Identity, safety: SafetyResult, outcome: Outcome, reply: Reply,
             ledger: Ledger) -> Validation:
    v: list[str] = []
    text = reply.text or ""

    unknown = [a for a in outcome.actions if a not in ACTION_VOCAB]
    if unknown:
        v.append(f"actions outside vocabulary: {unknown}")

    if safety.level == "emergency":
        if outcome.actions != ["escalate_emergency"]:
            v.append("emergency must have exactly [escalate_emergency]")
        if text != emergency_reply(identity.sender_name, reply.language):
            v.append("emergency reply is not the fixed emergency template")
        if ledger.of("prepare_refill_request", ok=True) or ledger.of("book_appointment", ok=True):
            v.append("routine work performed on an emergency")

    if not identity.verified:
        for c in ledger.calls:
            if c.name in PHI_READ_TOOLS and c.ok:
                v.append(f"PHI tool {c.name} returned data for an unverified sender")
        if identity.patient_id and text:
            leaked = _phi_terms(data, ledger, identity.patient_id, text)
            if leaked:
                v.append(f"reply to unverified sender contains record details: {leaked}")

    if "prepare_refill_for_approval" in outcome.actions and not ledger.of("prepare_refill_request", ok=True):
        v.append("prepare_refill_for_approval without a successful prepare_refill_request")
    for c in ledger.of("book_appointment", ok=True) + ledger.of("reschedule_appointment", ok=True):
        appt = c.result.get("booked") or c.result.get("rescheduled")
        if appt and (fmt_date(appt["date"], reply.language) not in text or
                     fmt_time(appt["start"], reply.language) not in text):
            v.append(f"reply does not state the confirmed slot {appt['date']} {appt['start']}")

    llm_paragraphs = reply.llm_paragraphs
    if llm_paragraphs:
        facts_text = str(reply.facts) + " " + data.clinic.today.isoformat()
        for p in llm_paragraphs:
            bad = _ungrounded(p, facts_text)
            if bad:
                v.append(f"LLM draft contains details not in tool facts: {bad}")
            if CLINICAL_ADVICE.search(p):
                v.append("LLM draft contains clinical-advice wording")
    return Validation(not v, v)

