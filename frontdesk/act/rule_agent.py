"""Deterministic workflow over the same MCP tools. The offline fallback and the baseline the LLM agent is
compared with. It follows the same written policy as the LLM agent's instructions."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from frontdesk.act.resolution import OutcomeItem, Resolution
from frontdesk.data import Clinic, Message
from frontdesk.policy.calendar import next_business_days
from frontdesk.policy.identity import Identity
from frontdesk.tools.client import Ledger, ToolClient
from frontdesk.understand.schema import Understanding


class RuleAgent:
    def __init__(self, tools: ToolClient, clinic: Clinic):
        self.tools = tools
        self.clinic = clinic

    async def _call(self, ledger: Ledger, name: str, **args: Any) -> dict[str, Any]:
        return await self.tools.call(ledger, name, {k: v for k, v in args.items() if v is not None}, by="rules")

    async def act(self, msg: Message, identity: Identity, und: Understanding, ledger: Ledger,
                  sender_facts: dict[str, Any], urgent_clinical: bool) -> Resolution:
        res = Resolution()
        intents = und.intents
        if "refill" in intents:
            await self._refill(ledger, und, res, sender_facts)
        if "cancel" in intents:
            await self._cancel(ledger, und, res)
        if "reschedule" in intents:
            await self._reschedule(ledger, und, res)
        if "schedule" in intents:
            await self._schedule(ledger, und, res)
        if "new_patient" in intents:
            await self._call(ledger, "get_clinic_info", topic="new_patients")
            d0 = self.clinic.today
            r = await self._call(ledger, "find_open_slots", date_from=d0.isoformat(),
                                 date_to=(d0 + timedelta(days=14)).isoformat(), visit_type="new_patient")
            res.offered_slot_ids += [s["slot_id"] for s in r.get("slots", [])[:3]]
            res.outcomes.append(OutcomeItem(intent="new_patient", outcome="answered"))
            res.outcomes.append(OutcomeItem(intent="new_patient", outcome="offered_new_patient_booking"))
        if "billing" in intents:
            await self._call(ledger, "route_to_queue", queue="billing", reason=und.summary or "Billing question")
            res.outcomes.append(OutcomeItem(intent="billing", outcome="routed_billing"))
        if "records" in intents:
            await self._call(ledger, "route_to_queue", queue="front_desk", reason=und.summary or "Records request")
            res.outcomes.append(OutcomeItem(intent="records", outcome="routed_front_desk"))
        if "admin" in intents:
            if _is_contact_change(msg.body):
                await self._call(ledger, "route_to_queue", queue="front_desk", reason="Contact detail change request")
                res.outcomes.append(OutcomeItem(intent="admin", outcome="routed_front_desk"))
            else:
                await self._call(ledger, "get_clinic_info", topic="all")
                res.outcomes.append(OutcomeItem(intent="admin", outcome="answered"))
        if "clinical" in intents and not any(i in intents for i in ("schedule", "refill")):
            q = "clinician_urgent" if urgent_clinical else "clinician_routine"
            await self._call(ledger, "route_to_queue", queue=q, reason=und.concern_summary or und.summary)
            res.outcomes.append(OutcomeItem(intent="clinical", outcome="routed_clinician"))
        if intents == ["other"]:
            res.outcomes.append(OutcomeItem(intent="other", outcome="acknowledged"))
        return res

    # ------------------------------------------------------------------ refills
    async def _refill(self, ledger: Ledger, und: Understanding, res: Resolution, facts: dict[str, Any]) -> None:
        meds = list(und.medications)
        if und.all_medications or not meds:
            listed = await self._call(ledger, "get_medications")
            active = [m["medication"] for m in listed.get("medications", [])]
            if und.all_medications or len(active) == 1:
                meds = active
        if not meds:
            await self._call(ledger, "route_to_queue", queue="front_desk", reason="Refill request: medication unclear")
            res.outcomes.append(OutcomeItem(intent="refill", outcome="routed_front_desk"))
            return
        for med in meds:
            e = await self._call(ledger, "check_refill_eligibility", medication=med)
            status = e.get("status")
            if status == "eligible":
                await self._call(ledger, "prepare_refill_request", medication=med)
                res.outcomes.append(OutcomeItem(intent="refill", outcome="refill_prepared", detail=med))
            elif status == "too_early":
                res.outcomes.append(OutcomeItem(intent="refill", outcome="refill_too_early", detail=med))
            elif status == "needs_visit":
                await self._call(ledger, "route_to_queue", queue="provider_refills",
                                 reason=f"{med}: not seen in 12 months - needs a visit")
                d0 = self.clinic.today
                r = await self._call(ledger, "find_open_slots", date_from=d0.isoformat(),
                                     date_to=(d0 + timedelta(days=14)).isoformat(), visit_type="follow_up")
                res.offered_slot_ids += [s["slot_id"] for s in r.get("slots", [])[:3]]
                res.outcomes.append(OutcomeItem(intent="refill", outcome="refill_needs_visit", detail=med))
            else:
                await self._call(ledger, "route_to_queue", queue="provider_refills",
                                 reason=f"{med}: {status} - {e.get('reason', '')}")
                res.outcomes.append(OutcomeItem(intent="refill", outcome="refill_to_provider", detail=med))

    # ------------------------------------------------------------------ scheduling
    def _window(self, und: Understanding) -> tuple[str, str]:
        tp, today = und.time_preference, self.clinic.today
        if tp.date_from:
            return tp.date_from, tp.date_to or tp.date_from
        if und.visit_type == "sick_visit":
            days = next_business_days(self.clinic, today, 2)
            return days[0].isoformat(), days[-1].isoformat()
        return today.isoformat(), (today + timedelta(days=14)).isoformat()

    async def _alternatives(self, ledger: Ledger, und: Understanding, visit_type: str, provider: str | None,
                            res: Resolution, intent: str) -> None:
        """Offer up to 3 slots closest to what was asked: same provider first (nearby days), then other providers
        on the requested day. Ranked by distance in days, then distance from the requested time."""
        d0, _ = self._window(und)
        start = date.fromisoformat(d0)
        want_min = _minutes(und.time_preference.exact_time) if und.time_preference.exact_time else None
        pool: list[dict[str, Any]] = []
        if provider:  # the requested day, then the following week
            for a, b in ((start, start), (start + timedelta(days=1), start + timedelta(days=14))):
                r = await self._call(ledger, "find_open_slots", date_from=a.isoformat(), date_to=b.isoformat(),
                                     visit_type=visit_type, provider=provider)
                pool += r.get("slots", [])
        r = await self._call(ledger, "find_open_slots", date_from=start.isoformat(), date_to=start.isoformat(),
                             visit_type=visit_type)
        pool += [s for s in r.get("slots", []) if s["provider"] != provider]

        def rank(s: dict[str, Any]) -> tuple[int, int, int]:
            day_gap = abs((date.fromisoformat(s["date"]) - start).days)
            time_gap = abs(_minutes(s["start"]) - want_min) if want_min is not None else _minutes(s["start"])
            return (0 if s["provider"] == provider else 1, day_gap, time_gap)

        same = sorted([s for s in pool if s["provider"] == provider], key=rank)[:2]
        other = sorted([s for s in pool if s["provider"] != provider], key=rank)
        chosen = same + other[: 3 - len(same)]
        res.offered_slot_ids += [s["slot_id"] for s in chosen]
        res.outcomes.append(OutcomeItem(intent=intent, outcome="offered_alternatives"))

    async def _schedule(self, ledger: Ledger, und: Understanding, res: Resolution) -> None:
        vt = und.visit_type or "follow_up"
        tp = und.time_preference
        d0, d1 = self._window(und)
        if tp.exact_time and tp.date_from and und.provider:
            c = await self._call(ledger, "check_slot", provider=und.provider, day=tp.date_from, start=tp.exact_time,
                                 visit_type=vt)
            if c.get("available"):
                b = await self._call(ledger, "book_appointment", slot_id=c["slot_id"])
                if b.get("ok"):
                    res.outcomes.append(OutcomeItem(intent="schedule", outcome="booked"))
                    return
            await self._alternatives(ledger, und, vt, und.provider, res, "schedule")
            return
        r = await self._call(ledger, "find_open_slots", date_from=d0, date_to=d1, visit_type=vt,
                             provider=und.provider, part_of_day=tp.part_of_day)
        slots = r.get("slots", [])
        if slots:
            b = await self._call(ledger, "book_appointment", slot_id=slots[0]["slot_id"])
            if b.get("ok"):
                res.outcomes.append(OutcomeItem(intent="schedule", outcome="booked"))
                return
        await self._alternatives(ledger, und, vt, und.provider, res, "schedule")

    async def _pick_existing(self, ledger: Ledger, und: Understanding) -> list[dict[str, Any]]:
        r = await self._call(ledger, "get_patient_appointments")
        appts = r.get("appointments", [])
        ex = und.existing_appointment
        if ex.date:
            appts = [a for a in appts if a["date"] == ex.date]
        if ex.hint:
            hinted = [a for a in appts if a["visit_type"].replace("_", " ") in ex.hint.lower()]
            appts = hinted or appts
        return appts

    async def _reschedule(self, ledger: Ledger, und: Understanding, res: Resolution) -> None:
        appts = await self._pick_existing(ledger, und)
        if len(appts) != 1:
            await self._call(ledger, "route_to_queue", queue="front_desk",
                             reason="Reschedule request: could not identify a single appointment")
            res.outcomes.append(OutcomeItem(intent="reschedule", outcome="routed_front_desk"))
            return
        a = appts[0]
        d0, d1 = self._window(und)
        r = await self._call(ledger, "find_open_slots", date_from=d0, date_to=d1, visit_type=a["visit_type"],
                             provider=und.provider or a["provider"], part_of_day=und.time_preference.part_of_day)
        slots = r.get("slots", [])
        if slots:
            m = await self._call(ledger, "reschedule_appointment", appointment_id=a["appointment_id"],
                                 slot_id=slots[0]["slot_id"])
            if m.get("ok"):
                res.outcomes.append(OutcomeItem(intent="reschedule", outcome="rescheduled"))
                return
        await self._alternatives(ledger, und, a["visit_type"], a["provider"], res, "reschedule")

    async def _cancel(self, ledger: Ledger, und: Understanding, res: Resolution) -> None:
        appts = await self._pick_existing(ledger, und)
        if not appts or (len(appts) > 1 and not und.existing_appointment.all_on_date):
            await self._call(ledger, "route_to_queue", queue="front_desk",
                             reason="Cancel request: could not identify which appointment")
            res.outcomes.append(OutcomeItem(intent="cancel", outcome="routed_front_desk"))
            return
        for a in appts:
            await self._call(ledger, "cancel_appointment", appointment_id=a["appointment_id"])
        res.outcomes.append(OutcomeItem(intent="cancel", outcome="cancelled"))


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _is_contact_change(body: str) -> bool:
    return bool(re.search(r"\b(change|update|new)\b.{0,20}\b(phone|number|address|email)\b", body, re.I))

