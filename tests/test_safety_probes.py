"""Deterministic safety gate on synthetic messages: emergencies in clear wording are caught without AI; negated,
historical, informational, quoted and figurative wording is deferred to the AI, and escalates if the AI fails."""

from __future__ import annotations

import pytest

from frontdesk.safety import combine, keyword_gate

EMERGENCIES = [
    ("cardiac", "I think I'm having a heart attack"),
    ("cardiac", "heart attack symptoms right now, arm numb"),
    ("cardiac", "pressure in my chest and I feel like passing out"),
    ("cardiac", "my chest feels tight and my jaw hurts"),
    ("cardiac typo", "crushing chest pian since an hour"),
    ("cardiac typo", "hart attack i think"),
    ("stroke", "my mom's face is drooping and her speech is slurred"),
    ("stroke", "he suddenly can't move his left arm and is confused"),
    ("stroke", "worst headache of my life came on suddenly"),
    ("stroke", "sudden loss of vision in one eye"),
    ("breathing", "I can't breathe"),
    ("breathing typo", "cant breath properly, lips turning blue"),
    ("breathing", "struggling to breathe since this morning"),
    ("breathing", "my son is choking"),
    ("breathing", "No, she is not breathing"),
    ("allergy", "my tongue is swelling after eating peanuts"),
    ("allergy", "face and lips swollen, hives everywhere, hard to swallow"),
    ("allergy", "allergic reaction, throat feels tight"),
    ("low sugar", "I'm diabetic, shaky sweaty and confused, sugar really low"),
    ("low sugar", "feel like my blood sugar dropped, very shaky and sweaty"),
    ("low sugar", "my glucose reading says LO"),
    ("low sugar", "I'm diabetic and I feel shaky, sweaty and confused right now, I think my sugar is really low."),
    ("seizure", "my daughter is having a seizure"),
    ("seizure", "he had a fit and is not responding"),
    ("unconscious", "she collapsed and won't wake up"),
    ("unconscious", "he isnt responding and his lips are blue"),
    ("head injury", "Mom fell this morning and hit her head, she seems confused now."),
    ("overdose", "I took too many of my sleeping pills"),
    ("overdose", "my toddler swallowed some of my pills"),
    ("bleeding", "I'm vomiting blood"),
    ("bleeding", "cut my hand and the bleeding won't stop"),
    ("bleeding", "on warfarin and my nose has been bleeding for an hour"),
    ("bleeding", "I'm pregnant and bleeding heavily"),
    ("self-harm", "I want to kill myself tonight"),
    ("self-harm", "I have a plan to end my life"),
    ("self-harm", "i dont want to live anymore"),
    ("self-harm", "This pain doesnt stop I wann hang myself to death"),
    ("spanish", "Tengo un dolor fuerte en el pecho"),
    ("spanish", "Creo que estoy teniendo un infarto"),
    ("spanish", "No puedo respirar"),
    ("spanish", "Se me cierra la garganta y tengo la cara hinchada"),
    ("spanish", "Mi hijo está convulsionando"),
    ("spanish", "Mi esposo se desmayó y no despierta"),
    ("spanish", "Quiero matarme"),
    ("mixed clause", "I dont know whats happening, my chest hurts so much"),
    ("recent, not history", "She had a seizure this morning"),
    ("recent, not history", "he had a stroke an hour ago, what do we do"),
    ("negation elsewhere", "It doesn't stop hurting and I want to kill myself"),
]

NOT_IMMEDIATE = [
    ("negation", "No chest pain, just a cough, can I book a visit?"),
    ("negation", "I'm not having trouble breathing, I just need my inhaler refilled"),
    ("negation", "never had a seizure, need a sports physical form"),
    ("negation", "I dont have an heart attack but my meds are over"),
    ("negation", "No tengo dolor en el pecho, solo necesito una cita"),
    ("history", "I had a heart attack in 2019, need a cardiology follow-up"),
    ("history", "history of stroke, please refill my aspirin"),
    ("history", "my father died of a heart attack, can I get a cholesterol check?"),
    ("history", "my dad had a stroke 3 years ago, can he get a flu shot?"),
    ("history", "my blood sugar is low sometimes, any diet tips?"),
    ("idiom", "I'm dying to get in before my trip"),
    ("idiom", "you nearly gave me a heart attack with that bill"),
    ("idiom", "this paperwork is killing me"),
    ("idiom", "I'm bored to death waiting for my results"),
    ("idiom", "I could kill myself for forgetting my appointment"),
    ("quote", "my friend said 'I can't breathe' during the movie, haha. Anyway, can I reschedule?"),
    ("information", "what are the signs of a stroke? asking for a school project"),
    ("ordinary", "I want to end my appointment early"),
    ("ordinary", "Can I get my chest x-ray results? My knee pain is better"),
    ("ordinary", "I hurt myself at the gym, need a sports injury visit"),
]


@pytest.mark.parametrize("category,text", EMERGENCIES)
def test_clear_emergencies_are_caught_without_ai(category, text):
    r = keyword_gate(text)
    assert r.level == "emergency", (category, text, r.as_dict())


@pytest.mark.parametrize("category,text", NOT_IMMEDIATE)
def test_negated_historical_figurative_and_quoted_wording_is_not_an_instant_emergency(category, text):
    assert keyword_gate(text).level != "emergency", (category, text)


@pytest.mark.parametrize("text", [t for c, t in NOT_IMMEDIATE if c != "ordinary"])
def test_deferred_red_flags_escalate_when_the_ai_is_unavailable(text):
    a = keyword_gate(text)
    if not a.pending:
        pytest.skip("no red-flag words at all")
    assert combine(a, None).level == "emergency"          # AI failed / timed out: never cleared silently
    assert combine(a, "routine").level != "emergency"     # AI all-clear releases it


def test_ai_emergency_judgement_still_escalates_deferred_wording():
    a = keyword_gate("I'm not sure, no chest pain really, but my left side is numb and I can't lift my arm")
    assert combine(a, "emergency").level == "emergency"


def test_low_mood_stays_same_day_urgent_not_emergency():
    a = keyword_gate("Refill sertraline please. Also I don't see the point of anything lately")
    assert a.level == "urgent_clinical"
    assert combine(a, None).level == "urgent_clinical"
