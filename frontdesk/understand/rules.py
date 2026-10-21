"""Rules baseline understander: keywords + date phrases. No network. Deliberately simple and general - it is the
baseline the LLM must beat, and the offline fallback. It must never be tuned to individual sample messages."""

from __future__ import annotations

import re
from datetime import date

from frontdesk.data import Clinic, Message
from frontdesk.policy.calendar import next_business_days
from frontdesk.policy.identity import Identity
from frontdesk.policy.medications import ALIASES
from frontdesk.understand.dates import parse_part_of_day, parse_point, parse_time
from frontdesk.understand.schema import ExistingAppointment, TimePreference, Understanding

KW = {
    "spam": r"\bunsubscribe\b|\breply stop\b|\bspecial offer\b|\blimited time\b|\bthis month only\b|\bpromo",
    "billing": r"\bbill(ed|ing)?\b|\bbalance\b|\bco-?pay\b|\binsurance\b|\bcharge[ds]?\b|\binvoice\b|\bpayment\b"
               r"|\bhow much\b|\bcost\b|\bfactura\b",
    "records": r"\b(send|email|fax|forward|copy|need|get)\b.{0,40}\b(records?|results|vaccination|immuni[sz]ation)\b"
               r"|\bcopy of\b|\bmedical records?\b",
    "new_patient": r"\bnew patients?\b|\baccept(ing)? new\b|\bpacientes nuevos\b",
    "cancel": r"\bcancel\w*\b|\bcancelar\b",
    "reschedule": r"\b(move|reschedule|change|push)\b.{0,30}\b(appointment|appt|physical|visit|cita)\b"
                  r"|\breprogramar\b|\bcambiar (mi )?cita\b",
    "refill": r"\brefill\w*\b|\bsend in more\b|\banother\b.{0,15}\bscript\b|\bprescription\b|\bscript\b"
              r"|\bnecesito más\b|\bnecesito mas\b|\breceta\b|\bout of (my )?(pills|meds|medication)\b|\brenew\b",
    "schedule": r"\bbook\w*\b|\bappointment\b|\bappt\b|\bcita\b|\bsee (dr|doctor|the doctor)\b|\bget (her|him|me)? ?seen\b"
                r"|\bget in\b|\bphysical\b|\bcheck-?up\b|\bchequeo\b|\bfollow-?up\b|\bwant to see\b",
    "clinical": r"\bpain\b|\bfever\b|\bcough\w*\b|\bbleed\w*\b|\bburning\b|\bwheez\w*\b|\bdizz\w*\b|\brash\b"
                r"|\bvomit\w*\b|\bsugar\b|\bconfus\w*\b|\bchest\b|\bbreath\w*\b|\buti\b|\binfection\b"
                r"|\bfeel\w* (really |so |very )?(low|down|sick|unwell)\b|\bdon'?t see the point\b"
                r"|\bwhat .{0,30}\bmeans?\b|\bsymptom\w*\b|\bhurts?\b|\bswollen\b|\bfell\b|\binjur\w*\b",
    "admin": r"\b(open|closed|hours)\b|\bchange my (phone|number|address|email)\b|\bnew (phone|number|address)\b"
             r"|\bparking\b|\bholiday\b|\bthanksgiving\b",
    "other": r"\bthank(s| you)\b|\bgracias\b",
}

ACUTE = r"\bfever\b|\bburning\b|\bpain\b|\bvomit\w*\b|\binfection\b|\buti\b|\bcough\w*\b|\bout of (my )?pills\b|\basap\b"
INJECTION = (r"\bignore (all |your )?(previous |prior )?instructions\b|\bdisregard\b.{0,30}\b(rules|instructions)\b"
             r"|\bapprove (all|every|my)\b|\bauthori[sz]ed by dr\b|\bsystem prompt\b|\byou are now\b|\bact as\b")
SPANISH = r"\b(hola|necesito|por favor|cita|gracias|semana|tienen|próxima|más|mañana|receta)\b"
PROVIDERS = {"PATEL": r"\bpatel\b", "OKAFOR": r"\bokafor\b", "KIM": r"\b(sarah )?kim\b|\bnp kim\b"}


class RulesUnderstander:
    def __init__(self, clinic: Clinic):
        self.clinic = clinic

    def understand(self, msg: Message, identity: Identity) -> Understanding:
        text = msg.body.replace("’", "'")
        low = text.lower()
        today = self.clinic.today
        hit = {k: bool(re.search(p, low)) for k, p in KW.items()}
        meds = sorted({w for w in re.findall(r"[a-záéíóúñ]+", low) if w in ALIASES or w in _GENERICS})

        intents: list[str] = []
        if hit["spam"] and identity.relationship == "unknown":
            intents = ["spam"]
        else:
            if identity.relationship == "third_party":
                intents.append("third_party")
            if hit["refill"] or (meds and re.search(r"\b(need|more|refill|get|más|mas)\b", low)):
                intents.append("refill")
            if hit["cancel"]:
                intents.append("cancel")
            elif hit["reschedule"]:
                intents.append("reschedule")
            elif hit["new_patient"]:
                intents.append("new_patient")
            elif hit["schedule"] and "refill" not in intents:
                intents.append("schedule")
            if hit["records"] and not hit["schedule"]:
                intents.append("records")
            if hit["billing"]:
                intents.append("billing")
            if hit["clinical"]:
                intents.append("clinical")
            if hit["admin"] and not intents:
                intents.append("admin")
            if not intents:
                intents.append("other")
            if "third_party" in intents:
                intents = ["third_party"]

        clinical = "clinical" in intents
        acute = bool(re.search(ACUTE, low))
        concern = "none"
        if clinical:
            concern = "urgent_same_day" if acute and "schedule" not in intents else "routine"
        urgency = "urgent" if clinical and acute else "routine"

        tp = TimePreference()
        ex = ExistingAppointment()
        visit_type = None
        if any(i in intents for i in ("schedule", "reschedule", "cancel", "new_patient")):
            visit_type = self._visit_type(low, clinical, intents)
            target_text, existing_text = text, None
            m = re.search(r"\b(?:from|on)\b(.*?)\bto\b(.*)", text, re.I)
            if "reschedule" in intents and m:
                existing_text, target_text = m.group(1), m.group(2)
            if existing_text:
                w = parse_point(existing_text, today, self.clinic)
                ex.date = w[0].isoformat() if w else None
            if "cancel" in intents:
                w = parse_point(text, today, self.clinic)
                ex.date = w[0].isoformat() if w else None
                ex.all_on_date = bool(re.search(r"\bsomething came up\b|\bcan'?t make it\b", low))
            anchor = date.fromisoformat(ex.date) if ex.date else None
            w = parse_point(target_text, today, self.clinic, anchor=anchor)
            if w is None and visit_type == "sick_visit":
                days = next_business_days(self.clinic, today, 2)
                w = (days[0], days[-1])
            if w:
                tp.date_from, tp.date_to = w[0].isoformat(), w[1].isoformat()
            tp.part_of_day = parse_part_of_day(target_text)  # type: ignore[assignment]
            tp.exact_time = parse_time(target_text)
            tp.is_exact = bool(tp.exact_time) or bool(re.search(r"\b\d{1,2}(st|nd|rd|th)\b", target_text))
        provider = next((p for p, pat in PROVIDERS.items() if re.search(pat, low)), None)

        lang = "es" if len(re.findall(SPANISH, low)) >= 2 else "en"
        suspicious = bool(re.search(INJECTION, low))
        question = text if any(i in intents for i in ("admin", "new_patient")) else None
        return Understanding(
            intents=intents, summary=text[:120], medications=meds,
            all_medications=bool(re.search(r"\ball (of )?my (medications|meds|prescriptions)\b", low)),
            provider=provider, visit_type=visit_type, time_preference=tp, existing_appointment=ex,
            question=question, urgency=urgency, clinical_concern=concern,
            concern_summary=text[:160] if clinical else None, suspicious_instruction=suspicious,
            suspicious_reason="instruction-like text in the message" if suspicious else None, language=lang,
        )

    @staticmethod
    def _visit_type(low: str, clinical: bool, intents: list[str]) -> str | None:
        if "new_patient" in intents:
            return "new_patient"
        if re.search(r"\b(physical|annual|check-?up|chequeo)\b", low):
            return "annual_physical"
        if re.search(r"\btelehealth|video|virtual\b", low):
            return "telehealth"
        if clinical:
            return "sick_visit"
        if re.search(r"\bfollow-?up\b", low):
            return "follow_up"
        return "follow_up" if "schedule" in intents else None


_GENERICS = {"metformin", "lisinopril", "atorvastatin", "sertraline", "alprazolam", "amlodipine", "cetirizine",
             "albuterol", "levothyroxine", "zolpidem", "omeprazole", "escitalopram", "donepezil", "warfarin",
             "oxycodone", "insulin", "amphetamine", "inhaler", "hydrocodone", "lorazepam", "clonazepam", "diazepam"}
