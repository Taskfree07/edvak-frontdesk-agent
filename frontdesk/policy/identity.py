"""Sender verification and subject resolution (clinic.json -> privacy_policy).

Verification = the message comes from the phone/email on file, or name + date of birth match, or it comes from an
authorised caregiver's phone (then the cared-for patient is the subject).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel

from frontdesk.data import Patient

Relationship = Literal["self", "caregiver", "self_unverified", "third_party", "unknown"]


class Identity(BaseModel):
    verified: bool
    relationship: Relationship
    patient_id: str | None           # the patient the message is about, when identifiable
    method: str                      # how we decided (for the trace and the UI)
    sender_name: str | None = None   # how to greet the sender
    patient_name: str | None = None
    notes: list[str] = []


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z' ]", "", (s or "").lower()).replace("'", "").strip()


def _contact_matches(contact: str, phone: str | None, email: str | None) -> bool:
    c = contact.strip().lower()
    if "@" in c:
        return bool(email) and c == email.lower()
    d = _digits(c)
    return bool(d) and bool(phone) and len(d) >= 7 and _digits(phone).endswith(d[-7:]) and d.endswith(_digits(phone)[-7:])


def _mentioned_patients(body: str, patients: list[Patient]) -> list[Patient]:
    text = _norm_name(body)
    return [p for p in patients if re.search(rf"\b{re.escape(_norm_name(p.name))}(s)?\b", text)]


def identify(
    patients: list[Patient],
    from_contact: str | None,
    from_name: str | None,
    stated_dob: date | None,
    body: str,
) -> Identity:
    contact = (from_contact or "").strip()

    # 1. Contact on file for a patient -> verified self (unless they clearly write about someone else).
    if contact:
        own = [p for p in patients if _contact_matches(contact, p.phone, p.email)]
        if len(own) == 1:
            me = own[0]
            others = [p for p in _mentioned_patients(body, patients) if p.id != me.id]
            if others and not (others[0].authorised_caregiver and
                               _contact_matches(contact, others[0].authorised_caregiver.phone, None)):
                o = others[0]
                return Identity(verified=False, relationship="third_party", patient_id=o.id,
                                method="sender verified as another patient; asks about a patient they are not "
                                       "an authorised caregiver for",
                                sender_name=me.first_name, patient_name=o.name)
            cg = me.authorised_caregiver
            if cg and _contact_matches(contact, cg.phone, None) and _norm_name(from_name) != _norm_name(me.name):
                # Shared number (e.g. a child's record lists the parent's phone): the sender is the caregiver.
                return Identity(verified=True, relationship="caregiver", patient_id=me.id,
                                method=f"authorised caregiver on file ({cg.relationship}); shared phone",
                                sender_name=cg.name.split()[0], patient_name=me.name)
            return Identity(verified=True, relationship="self", patient_id=me.id, method="contact on file",
                            sender_name=me.first_name, patient_name=me.name)

        # 2. Authorised caregiver's phone -> verified, subject is the cared-for patient.
        cared = [p for p in patients if p.authorised_caregiver and
                 _contact_matches(contact, p.authorised_caregiver.phone, None)]
        if len(cared) == 1 or (len(cared) > 1 and len(set(_mentioned_patients(body, cared))) == 1):
            subj = cared[0] if len(cared) == 1 else _mentioned_patients(body, cared)[0]
            cg = subj.authorised_caregiver
            return Identity(verified=True, relationship="caregiver", patient_id=subj.id,
                            method=f"authorised caregiver on file ({cg.relationship})",
                            sender_name=cg.name.split()[0], patient_name=subj.name)

    # 3. Name + date of birth.
    by_name = [p for p in patients if from_name and _norm_name(p.name) == _norm_name(from_name)]
    if len(by_name) == 1:
        p = by_name[0]
        if stated_dob is not None and stated_dob == p.dob:
            return Identity(verified=True, relationship="self", patient_id=p.id, method="name + date of birth match",
                            sender_name=p.first_name, patient_name=p.name)
        why = "date of birth does not match the record" if stated_dob else "no date of birth given"
        return Identity(verified=False, relationship="self_unverified", patient_id=p.id,
                        method=f"name matches a patient but contact is not on file and {why}",
                        sender_name=(from_name or "").split()[0] or None, patient_name=p.name)
    if len(by_name) > 1:
        return Identity(verified=False, relationship="self_unverified", patient_id=None,
                        method="name matches several patients", sender_name=(from_name or "").split()[0] or None)

    # 4. Unknown sender writing about a patient on file -> third party.
    mentioned = _mentioned_patients(body, patients)
    if mentioned:
        o = mentioned[0]
        return Identity(verified=False, relationship="third_party", patient_id=o.id,
                        method="sender not on file; message is about a patient on file",
                        sender_name=(from_name or "").split()[0] or None, patient_name=o.name)

    return Identity(verified=False, relationship="unknown", patient_id=None, method="sender not on file",
                    sender_name=(from_name or "").split()[0] or None)
