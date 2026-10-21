"""Booking rules (clinic.json -> booking_rules) as code: slot validation and slot search."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from frontdesk.data import Appointment, Clinic
from frontdesk.policy.calendar import (
    from_minutes, grid_starts, is_holiday, is_open_day, on_grid, overlaps_lunch, to_minutes, within_hours,
)

PartOfDay = Literal["morning", "afternoon", "any"]
MAX_SLOTS = 8


class Slot(BaseModel):
    slot_id: str
    provider: str
    date: date
    start: str
    end: str
    visit_type: str

    @staticmethod
    def make_id(provider: str, d: date, start: str, visit_type: str) -> str:
        return f"{provider}_{d.strftime('%Y%m%d')}_{start.replace(':', '')}_{visit_type}"

    @classmethod
    def parse_id(cls, slot_id: str, clinic: Clinic) -> "Slot":
        provider, ymd, hhmm, visit_type = slot_id.split("_", 3)
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        start = f"{hhmm[:2]}:{hhmm[2:]}"
        dur = clinic.visit_types[visit_type]
        return cls(slot_id=slot_id, provider=provider, date=d, start=start,
                   end=from_minutes(to_minutes(start) + dur), visit_type=visit_type)


def validate_slot(
    clinic: Clinic,
    appointments: list[Appointment],
    provider: str,
    d: date,
    start: str,
    visit_type: str,
    not_before: tuple[date, int] | None = None,
    ignore_appointment_id: str | None = None,
) -> list[str]:
    """Return every rule the slot breaks (empty list = bookable)."""
    problems: list[str] = []
    prov = clinic.provider(provider)
    if prov is None:
        return [f"Unknown provider '{provider}'."]
    if visit_type not in clinic.visit_types:
        return [f"Unknown visit type '{visit_type}'."]
    try:
        start_m = to_minutes(start)
    except ValueError:
        return [f"Invalid start time '{start}'."]
    end_m = start_m + clinic.visit_types[visit_type]

    if is_holiday(clinic, d):
        problems.append(f"{d.isoformat()} is a clinic holiday.")
    elif not is_open_day(clinic, d):
        problems.append(f"The clinic is closed on {d.strftime('%A')}s.")
    for a, b in prov.leave:
        if a <= d <= b:
            problems.append(f"{prov.name} is on leave {a.isoformat()} to {b.isoformat()}.")
    if not on_grid(start):
        problems.append("Appointments must start on the hour or at :20 / :40.")
    if not within_hours(clinic, start_m, end_m):
        problems.append(f"Outside clinic hours ({clinic.hours.open}-{clinic.hours.close}).")
    if overlaps_lunch(clinic, start_m, end_m):
        problems.append(f"Overlaps lunch ({clinic.hours.lunch[0]}-{clinic.hours.lunch[1]}).")
    if not_before is not None:
        nd, nm = not_before
        if d < nd or (d == nd and start_m <= nm):
            problems.append("That time is in the past.")
    for a in appointments:
        if a.status != "booked" or a.provider != provider or a.date != d or a.id == ignore_appointment_id:
            continue
        if start_m < to_minutes(a.end) and to_minutes(a.start) < end_m:
            problems.append(f"{prov.name} already has an appointment {a.start}-{a.end} on {d.isoformat()}.")
            break
    return problems


def find_open_slots(
    clinic: Clinic,
    appointments: list[Appointment],
    providers: list[str],
    date_from: date,
    date_to: date,
    visit_type: str,
    part_of_day: PartOfDay = "any",
    not_before: tuple[date, int] | None = None,
    limit: int = MAX_SLOTS,
) -> list[Slot]:
    """Valid slots, earliest first per provider; providers are searched in preference order.

    With several providers the result is grouped: all of the first provider's slots (up to `limit`), then the next
    provider's, so the preferred provider's earliest slot is always first.
    """
    if visit_type not in clinic.visit_types:
        return []
    lunch0, lunch1 = (to_minutes(x) for x in clinic.hours.lunch)
    out: list[Slot] = []
    per_provider = max(1, limit // max(1, len(providers))) if len(providers) > 1 else limit
    for p in providers:
        found = 0
        d = date_from
        while d <= date_to and found < per_provider:
            for m in grid_starts(clinic):
                if part_of_day == "morning" and m >= lunch0:
                    continue
                if part_of_day == "afternoon" and m < lunch1:
                    continue
                start = from_minutes(m)
                if not validate_slot(clinic, appointments, p, d, start, visit_type, not_before):
                    out.append(Slot(slot_id=Slot.make_id(p, d, start, visit_type), provider=p, date=d, start=start,
                                    end=from_minutes(m + clinic.visit_types[visit_type]), visit_type=visit_type))
                    found += 1
                    if found >= per_provider:
                        break
            d += timedelta(days=1)
    return out[:limit]
