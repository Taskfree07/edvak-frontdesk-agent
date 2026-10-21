"""Stage 5a: enforce mandatory follow-ups (code), then derive actions, routing and reply items from FACTS in the
tool ledger. The LLM's resolution is only used where it adds information the ledger cannot (e.g. which of the
found slots it chose to offer), and even then it is checked against the ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from frontdesk.act.resolution import Resolution
from frontdesk.data import Clinic
from frontdesk.policy.identity import Identity
from frontdesk.safety import SafetyResult
from frontdesk.tools.client import Ledger, ToolClient
from frontdesk.understand.schema import Understanding

QUEUE_ACTION = {"clinician_urgent": "route_to_clinician", "clinician_routine": "route_to_clinician",
                "provider_refills": "route_to_provider", "billing": "route_to_billing",
                "front_desk": "route_to_front_desk"}
ACTION_ORDER = ["escalate_emergency", "route_to_clinician", "route_to_provider", "route_to_billing",
                "route_to_front_desk", "prepare_refill_for_approval", "refill_needs_visit", "refill_too_early",
                "book_appointment", "reschedule_appointment", "cancel_appointment", "offer_appointment",
                "offer_alternatives", "offer_new_patient_booking", "request_verification", "decline_disclosure",
                "flag_suspicious_instruction", "answer_directly", "acknowledge", "no_action"]
PHI_INTENTS = {"refill", "schedule", "reschedule", "cancel", "records", "clinical"}


@dataclass
class ReplyItem:
    kind: str                 # see reply/templates.py
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    actions: list[str]
    routed_to: list[str]
    items: list[ReplyItem]
    enforced: list[str] = field(default_factory=list)
    resolution_issues: list[str] = field(default_factory=list)


def _refill_facts(ledger: Ledger) -> list[dict[str, Any]]:
    """One entry per medication: latest eligibility status and whether a request was prepared."""
    facts: dict[str, dict[str, Any]] = {}
    for c in ledger.calls:
        if c.name == "check_refill_eligibility" and c.ok:
            key = c.result.get("medication") or c.result.get("requested_as")
            facts[key] = {**c.result, "prepared": facts.get(key, {}).get("prepared", False)}
        if c.name == "prepare_refill_request" and c.ok:
            key = c.result["medication"]
            facts.setdefault(key, {"status": "eligible", "medication": key})
            facts[key]["prepared"] = True
            facts[key]["status"] = "eligible"
    return list(facts.values())


def _slots_seen(ledger: Ledger) -> dict[str, dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for c in ledger.calls:
        if c.name == "find_open_slots" and c.ok:
            for s in c.result.get("slots", []):
                seen[s["slot_id"]] = s
    return seen


async def enforce_followups(tools: ToolClient, ledger: Ledger, clinic: Clinic, identity: Identity,
                            und: Understanding, safety: SafetyResult, facts: dict[str, Any]) -> list[str]:
    """Policy steps the agent must not skip. Done here, through the same MCP tools, marked by='code'."""
    done: list[str] = []
    routed = {c.result.get("queue") for c in ledger.of("route_to_queue", ok=True)}

    async def route(queue: str, reason: str) -> None:
        await tools.call(ledger, "route_to_queue", {"queue": queue, "reason": reason}, by="code")
        routed.add(queue)
        done.append(f"route_to_queue({queue}): {reason}")

    for f in _refill_facts(ledger):
        st = f.get("status")
        if st in ("controlled", "no_refills", "not_on_list", "needs_visit") and "provider_refills" not in routed:
            await route("provider_refills", f"Refill {f.get('medication') or f.get('requested_as')}: {st}")
        if st == "needs_visit" and not ledger.of("find_open_slots", ok=True):
            d0 = clinic.today
            await tools.call(ledger, "find_open_slots", {
                "date_from": d0.isoformat(), "date_to": (d0 + timedelta(days=14)).isoformat(),
                "visit_type": "follow_up", "provider": facts.get("primary_provider")}, by="code")
            done.append("find_open_slots: appointment offer for a refill that needs a visit")
    if safety.level == "urgent_clinical" and "clinician_urgent" not in routed:
        await route("clinician_urgent", f"Same-day clinical concern: {', '.join(safety.urgent_hits) or und.concern_summary}")
    booked = bool(ledger.of("book_appointment", ok=True))
    if ("clinical" in und.intents and identity.verified and not booked
            and not routed & {"clinician_urgent", "clinician_routine", "provider_refills"}):
        await route("clinician_routine", und.concern_summary or "Clinical question")
    if "billing" in und.intents and "billing" not in routed:
        await route("billing", und.summary or "Billing question")
    if "records" in und.intents and identity.verified and "front_desk" not in routed:
        await route("front_desk", und.summary or "Records request")
    return done


def derive(identity: Identity, safety: SafetyResult, und: Understanding | None, ledger: Ledger,
           resolution: Resolution | None, enforced: list[str]) -> Outcome:
    actions: list[str] = []
    routed: list[str] = []
    items: list[ReplyItem] = []
    issues: list[str] = []

    def add(a: str) -> None:
        if a not in actions:
            actions.append(a)

    def route(q: str) -> None:
        if q not in routed:
            routed.append(q)

    if safety.level == "emergency":
        route("clinician_urgent")
        return Outcome(["escalate_emergency"], routed, [ReplyItem("emergency")], enforced)

    intents = und.intents if und else []
    if identity.relationship == "third_party":
        add("decline_disclosure")
        items.append(ReplyItem("decline_disclosure"))
    elif not identity.verified and identity.patient_id and (set(intents) & PHI_INTENTS or not intents):
        add("request_verification")
        items.append(ReplyItem("verification"))
    if und and und.suspicious_instruction:
        add("flag_suspicious_instruction")

    # Refills
    for f in _refill_facts(ledger):
        med = f.get("medication") or f.get("requested_as")
        st = f.get("status")
        if f.get("prepared"):
            add("prepare_refill_for_approval")
            route("provider_refills")
            items.append(ReplyItem("refill_prepared", {"medication": med}))
        elif st == "too_early":
            add("refill_too_early")
            items.append(ReplyItem("refill_too_early", {"medication": med, "date": f.get("earliest_request_date")}))
        elif st == "needs_visit":
            add("refill_needs_visit")
            add("offer_appointment")
            items.append(ReplyItem("refill_needs_visit", {"medication": med}))
        elif st in ("controlled", "no_refills", "not_on_list"):
            items.append(ReplyItem("refill_to_provider", {"medication": med, "status": st}))

    # Appointments
    for c in ledger.of("book_appointment", ok=True):
        add("book_appointment")
        items.append(ReplyItem("booked", c.result["booked"]))
    for c in ledger.of("reschedule_appointment", ok=True):
        add("reschedule_appointment")
        items.append(ReplyItem("rescheduled", {"previous": c.result["previous"], "new": c.result["rescheduled"]}))
    cancelled = [c.result["cancelled"] for c in ledger.of("cancel_appointment", ok=True)]
    if cancelled:
        add("cancel_appointment")
        items.append(ReplyItem("cancelled", {"appointments": cancelled}))

    # Offers: only slots that a tool actually returned.
    seen = _slots_seen(ledger)
    claimed = [s for s in (resolution.offered_slot_ids if resolution else []) if s in seen]
    bogus = [s for s in (resolution.offered_slot_ids if resolution else []) if s not in seen]
    if bogus:
        issues.append(f"resolution offered slots no tool returned: {bogus}")
    outcomes = {o.outcome for o in resolution.outcomes} if resolution else set()
    if "refill_needs_visit" in actions:
        offer = claimed[:3] or list(seen)[:3]
        if items and offer:
            for it in items:
                if it.kind == "refill_needs_visit":
                    it.data["slots"] = [seen[s] for s in offer]
    wants_booking = bool(set(intents) & {"schedule", "reschedule"})
    if wants_booking and not ({"book_appointment", "reschedule_appointment"} & set(actions)) \
            and identity.verified:
        offer = claimed[:3] or list(seen)[:3]
        if offer:
            add("offer_alternatives")
            requested = [c.result for c in ledger.of("check_slot", ok=True) if not c.result.get("available")]
            items.append(ReplyItem("alternatives", {"slots": [seen[s] for s in offer],
                                                    "problems": requested[0].get("problems", []) if requested else
                                                    _notes(ledger)}))
        elif "front_desk" not in {c.result.get("queue") for c in ledger.of("route_to_queue", ok=True)}:
            issues.append("booking requested but no slot found or offered")
    if "new_patient" in intents and not identity.verified:
        add("answer_directly")
        add("offer_new_patient_booking")
        np_slots = [seen[s] for s in (claimed or [s for s in seen if seen[s]["visit_type"] == "new_patient"])][:3]
        items.append(ReplyItem("new_patient", {"slots": np_slots, "facts": _info(ledger)}))

    # Routing (explicit routes that created a queue item; prepared refills already routed above)
    for c in ledger.of("route_to_queue", ok=True):
        q = c.result.get("queue")
        if c.result.get("created") or q not in routed:
            if q == "provider_refills" and not any(
                    f.get("status") in ("controlled", "no_refills", "not_on_list", "needs_visit")
                    for f in _refill_facts(ledger)) and "prepare_refill_for_approval" in actions:
                continue  # redundant explicit route after a successful prepare
            add(QUEUE_ACTION[q])
            route(q)
            if q != "provider_refills":
                items.append(ReplyItem("routed", {"queue": q}))

    # Answers to general questions: grounded in get_clinic_info.
    answered = "answered" in outcomes or (und and und.question and set(intents) & {"admin", "other"})
    if answered and ledger.of("get_clinic_info", ok=True) and "answer_directly" not in actions \
            and "route_to_front_desk" not in actions:
        add("answer_directly")
        items.append(ReplyItem("answer", {"question": und.question if und else "", "facts": _info(ledger)}))

    if "spam" in intents and not actions:
        add("no_action")
        items = [ReplyItem("no_reply")]
    if not actions:
        if set(intents) <= {"other"} or "acknowledged" in outcomes:
            add("acknowledge")
            items.append(ReplyItem("acknowledge"))
        else:  # nothing could be done safely: a human looks at it
            add("route_to_front_desk")
            route("front_desk")
            items.append(ReplyItem("routed", {"queue": "front_desk"}))
            issues.append("no action derived - fell back to front desk")

    ordered = sorted(actions, key=ACTION_ORDER.index)
    return Outcome(ordered, routed, items, enforced, issues)


def _info(ledger: Ledger) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in ledger.of("get_clinic_info", ok=True):
        out.update({k: v for k, v in c.result.items() if k not in ("ok", "topic")})
    return out


def _notes(ledger: Ledger) -> list[str]:
    notes: list[str] = []
    for c in ledger.of("find_open_slots", ok=True):
        notes += [n for n in c.result.get("notes", []) if n not in notes]
    return notes
