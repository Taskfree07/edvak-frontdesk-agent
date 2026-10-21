"""Sender verification (privacy policy) and the safety gate."""

from __future__ import annotations

from datetime import date

import pytest

from frontdesk.policy.identity import identify
from frontdesk.safety import combine, keyword_gate


# ---------------------------------------------------------------- identity
def test_contact_on_file_is_verified(data):
    i = identify(data.patients, "555-0102", "Robert Chen", None, "refill please")
    assert i.verified and i.patient_id == "P002" and i.relationship == "self"


def test_email_case_insensitive(data):
    assert identify(data.patients, "Robert.Chen@Example.com", None, None, "x").patient_id == "P002"


def test_caregiver_phone_makes_patient_the_subject(data):
    i = identify(data.patients, "555-0177", "Claire Ellis", None, "Mom's refill")
    assert i.verified and i.relationship == "caregiver" and i.patient_id == "P017" and i.sender_name == "Claire"


def test_shared_phone_with_caregiver_greets_the_caregiver(data):
    i = identify(data.patients, "555-0109", "Anna Rossi", None, "Sofia has a fever")
    assert i.verified and i.relationship == "caregiver" and i.patient_id == "P009" and i.sender_name == "Anna"


def test_name_and_dob_verify(data):
    i = identify(data.patients, "new@example.org", "Robert Chen", date(1971, 11, 2), "send results")
    assert i.verified and i.method.startswith("name + date of birth")


def test_wrong_dob_is_not_verified_but_subject_known(data):
    i = identify(data.patients, "new@example.org", "Robert Chen", date(1971, 11, 20), "send results")
    assert not i.verified and i.relationship == "self_unverified" and i.patient_id == "P002"


def test_unknown_third_party(data):
    i = identify(data.patients, "555-0888", "Mike", None, "Does my wife Grace Park have an appointment?")
    assert not i.verified and i.relationship == "third_party" and i.patient_id == "P013"


def test_verified_patient_asking_about_someone_else_is_third_party(data):
    i = identify(data.patients, "555-0102", "Robert Chen", None, "When is Grace Park's next appointment?")
    assert not i.verified and i.relationship == "third_party" and i.patient_id == "P013"


def test_unknown_sender(data):
    i = identify(data.patients, "x@example.net", "Jessica Parker", None, "new patients?")
    assert not i.verified and i.patient_id is None and i.relationship == "unknown"


# ---------------------------------------------------------------- safety gate
@pytest.mark.parametrize("text", [
    "Woke up with crushing chest pain going into my left arm",
    "I'm wheezing and can't finish sentences",
    "My blood sugar reading was 45 and I'm shaky",
    "Mom fell and hit her head, she seems confused now",
    "my gums are bleeding a lot and I have dark stools",
    "his face is drooping and his speech is slurred",
    "my throat feels like it's closing after the new pill",
    "I want to kill myself",
    "I took all my pills an hour ago",
    "She had a seizure this morning",
])
def test_emergencies_escalate_before_llm(text):
    assert keyword_gate(text).level == "emergency"


@pytest.mark.parametrize("text", [
    "Need my levothyroxine refilled",
    "I'm running low on lisinopril",
    "Can I book a physical next week?",
    "Need zolpidem refill, can't sleep",
    "What's my balance?",
])
def test_routine_messages_do_not_escalate(text):
    assert keyword_gate(text).level == "none"


def test_passive_low_mood_is_urgent_not_emergency():
    r = keyword_gate("I've been feeling really low lately and some days I don't see the point of anything.")
    assert r.level == "urgent_clinical"


def test_idiom_needs_llm_clearance():
    a = keyword_gate("I'm dying to get in before my trip lol")
    assert a.level == "none" and a.idiom_hits
    assert combine(a, "none").level == "none"            # LLM says not clinical -> cleared
    assert combine(a, None).level == "emergency"         # no LLM available -> err toward escalation
    assert combine(a, "emergency").level == "emergency"


def test_llm_can_escalate_but_never_downgrade_keywords():
    assert combine(keyword_gate("fever and stiff neck, very confused"), "emergency").level == "emergency"
    assert combine(keyword_gate("crushing chest pain"), "none").level == "emergency"
