"""Refill, scheduling, medication matching and calendar rules - written against clinic.json policy."""

from __future__ import annotations

from datetime import date, timedelta

from frontdesk.data import Medication, Patient
from frontdesk.policy.calendar import next_business_days
from frontdesk.policy.medications import canonical, match_medication
from frontdesk.policy.refill import check_refill
from frontdesk.policy.scheduling import find_open_slots, validate_slot

TODAY = date(2026, 11, 9)


def _patient(last_visit: date) -> Patient:
    return Patient(id="X", name="Test Person", dob=date(1980, 1, 1), phone="555-9999", email="t@example.com",
                   primary_provider="PATEL", last_visit=last_visit)


def _med(**kw) -> Medication:
    base = dict(patient_id="X", medication="Lisinopril 10 mg", controlled_schedule=None, days_supply=30,
                last_filled=TODAY - timedelta(days=25), refills_remaining=2, prescriber="PATEL")
    base.update(kw)
    return Medication(**base)


# ---------------------------------------------------------------- refill policy
def test_refill_eligible():
    assert check_refill(_patient(TODAY - timedelta(days=30)), _med(), "lisinopril", TODAY).status == "eligible"


def test_refill_not_on_list():
    assert check_refill(_patient(TODAY), None, "ibuprofen", TODAY).status == "not_on_list"


def test_refill_controlled_wins_over_everything():
    d = check_refill(_patient(TODAY - timedelta(days=800)), _med(controlled_schedule="C-II", refills_remaining=0),
                     "x", TODAY)
    assert d.status == "controlled"


def test_refill_needs_visit_after_12_months():
    assert check_refill(_patient(date(2025, 11, 9)), _med(), "x", TODAY).status == "needs_visit"
    assert check_refill(_patient(date(2025, 11, 10)), _med(), "x", TODAY).status == "eligible"


def test_refill_no_refills():
    assert check_refill(_patient(TODAY), _med(refills_remaining=0), "x", TODAY).status == "no_refills"


def test_refill_seven_day_window_boundaries():
    # run-out = last_filled + 30; earliest = run-out - 7
    on_edge = _med(last_filled=TODAY - timedelta(days=23))      # runs out in 7 days -> exactly allowed
    early = _med(last_filled=TODAY - timedelta(days=22))        # runs out in 8 days -> too early
    assert check_refill(_patient(TODAY), on_edge, "x", TODAY).status == "eligible"
    d = check_refill(_patient(TODAY), early, "x", TODAY)
    assert d.status == "too_early" and d.earliest_request_date == TODAY + timedelta(days=1)


def test_sample_refill_dates(data):
    p15, p17 = data.patient("P015"), data.patient("P017")
    om = match_medication("omeprazole", data.meds_for("P015"))
    dz = match_medication("donepezil", data.meds_for("P017"))
    assert check_refill(p15, om, "omeprazole", TODAY).earliest_request_date == date(2026, 11, 17)
    assert check_refill(p17, dz, "donepezil", TODAY).earliest_request_date == date(2026, 11, 21)


# ---------------------------------------------------------------- medication names
def test_medication_brand_and_spanish_names(data):
    assert match_medication("Xanax", data.meds_for("P006")).medication.startswith("Alprazolam")
    assert match_medication("metformina", data.meds_for("P001")).medication.startswith("Metformin")
    assert match_medication("my inhaler", data.meds_for("P010")).medication.startswith("Albuterol")
    assert match_medication("ibuprofen", data.meds_for("P002")) is None
    assert canonical("Lisinopril-HCTZ 20/12.5 mg") == "lisinopril-hctz"


# ---------------------------------------------------------------- scheduling
def test_slot_rules(data):
    c, appts = data.clinic, data.appointments
    assert validate_slot(c, appts, "KIM", date(2026, 11, 13), "09:10", "follow_up")  # off grid
    assert any("lunch" in p.lower() for p in validate_slot(c, appts, "KIM", date(2026, 11, 13), "11:40",
                                                           "annual_physical"))  # 40 min crosses 12:00
    assert any("holiday" in p for p in validate_slot(c, appts, "PATEL", date(2026, 11, 27), "09:00", "follow_up"))
    assert any("leave" in p for p in validate_slot(c, appts, "OKAFOR", date(2026, 11, 18), "09:00", "follow_up"))
    assert any("closed" in p for p in validate_slot(c, appts, "PATEL", date(2026, 11, 14), "09:00", "follow_up"))
    assert any("already has" in p for p in validate_slot(c, appts, "OKAFOR", date(2026, 11, 12), "14:00",
                                                         "follow_up"))
    assert any("Outside" in p for p in validate_slot(c, appts, "KIM", date(2026, 11, 13), "16:40",
                                                     "annual_physical"))  # ends 17:20


def test_past_slots_excluded(data):
    c = data.clinic
    assert any("past" in p for p in validate_slot(c, [], "KIM", TODAY, "09:00", "follow_up", (TODAY, 10 * 60)))


def test_find_open_slots_are_all_valid(data):
    c, appts = data.clinic, data.appointments
    slots = find_open_slots(c, appts, ["PATEL", "OKAFOR", "KIM"], TODAY, TODAY + timedelta(days=18),
                            "annual_physical", limit=60)
    assert slots
    for s in slots:
        assert not validate_slot(c, appts, s.provider, s.date, s.start, s.visit_type)
    assert all(s.date not in c.holidays for s in slots)


def test_find_open_slots_part_of_day(data):
    am = find_open_slots(data.clinic, data.appointments, ["KIM"], date(2026, 11, 13), date(2026, 11, 13),
                         "follow_up", "morning")
    pm = find_open_slots(data.clinic, data.appointments, ["KIM"], date(2026, 11, 13), date(2026, 11, 13),
                         "follow_up", "afternoon")
    assert all(s.start < "12:00" for s in am) and all(s.start >= "13:00" for s in pm)


def test_business_days_skip_weekends_and_holidays(data):
    days = next_business_days(data.clinic, date(2026, 11, 25), 2)
    assert days == [date(2026, 11, 25), date(2026, 11, 30)]
