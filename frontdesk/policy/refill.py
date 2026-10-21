"""The clinic refill policy (clinic.json -> refill_policy), as code."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from frontdesk.data import Medication, Patient

RefillStatus = Literal["eligible", "controlled", "needs_visit", "no_refills", "too_early", "not_on_list"]

EARLY_WINDOW_DAYS = 7


class RefillDecision(BaseModel):
    status: RefillStatus
    medication: str | None
    requested_as: str
    reason: str
    prescriber: str | None = None
    run_out_date: date | None = None
    earliest_request_date: date | None = None
    controlled_schedule: str | None = None

    @property
    def eligible(self) -> bool:
        return self.status == "eligible"


def one_year_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - 1, day=28)


def check_refill(patient: Patient, med: Medication | None, requested_as: str, today: date) -> RefillDecision:
    """Apply the five eligibility rules in a fixed order. The first failing rule decides the status."""
    if med is None:
        return RefillDecision(
            status="not_on_list", medication=None, requested_as=requested_as,
            reason="This medication is not on the patient's active medication list.",
        )
    base = dict(medication=med.medication, requested_as=requested_as, prescriber=med.prescriber,
                controlled_schedule=med.controlled_schedule)
    run_out = med.last_filled + timedelta(days=med.days_supply)
    earliest = run_out - timedelta(days=EARLY_WINDOW_DAYS)
    base.update(run_out_date=run_out)

    if med.controlled_schedule:
        return RefillDecision(status="controlled", reason=(
            f"Controlled substance (schedule {med.controlled_schedule}); the agent never prepares these. "
            f"Route to the prescribing provider."), **base)
    if patient.last_visit <= one_year_before(today):
        return RefillDecision(status="needs_visit", reason=(
            f"Last visit {patient.last_visit.isoformat()} is more than 12 months ago. "
            f"Offer an appointment and route to the provider."), **base)
    if med.refills_remaining <= 0:
        return RefillDecision(status="no_refills", reason=(
            "No refills remaining. Route to the provider for a new prescription."), **base)
    if today < earliest:
        return RefillDecision(status="too_early", earliest_request_date=earliest, reason=(
            f"Supply runs out {run_out.isoformat()}; a request can be prepared from {earliest.isoformat()}."), **base)
    return RefillDecision(status="eligible", reason="All five refill rules pass.", **base)
