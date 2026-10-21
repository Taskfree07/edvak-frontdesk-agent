"""Clinic calendar: business days, opening hours, lunch, the booking grid."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from frontdesk.data import Clinic

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GRID_MINUTES = (0, 20, 40)


def to_minutes(t: time | str) -> int:
    if isinstance(t, str):
        t = time.fromisoformat(t)
    return t.hour * 60 + t.minute


def from_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def is_holiday(clinic: Clinic, d: date) -> bool:
    return d in clinic.holidays


def is_open_day(clinic: Clinic, d: date) -> bool:
    return WEEKDAY_NAMES[d.weekday()] in clinic.hours.days and not is_holiday(clinic, d)


def next_business_days(clinic: Clinic, start: date, n: int, include_start: bool = True) -> list[date]:
    """The first `n` open days from `start` (inclusive by default)."""
    out: list[date] = []
    d = start if include_start else start + timedelta(days=1)
    while len(out) < n:
        if is_open_day(clinic, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def grid_starts(clinic: Clinic) -> list[int]:
    """Every :00/:20/:40 start time (in minutes) inside opening hours."""
    open_m, close_m = to_minutes(clinic.hours.open), to_minutes(clinic.hours.close)
    return [m for m in range(open_m, close_m) if m % 60 in GRID_MINUTES]


def on_grid(start: str) -> bool:
    return to_minutes(start) % 60 in GRID_MINUTES


def within_hours(clinic: Clinic, start_m: int, end_m: int) -> bool:
    return to_minutes(clinic.hours.open) <= start_m and end_m <= to_minutes(clinic.hours.close)


def overlaps_lunch(clinic: Clinic, start_m: int, end_m: int) -> bool:
    l0, l1 = (to_minutes(x) for x in clinic.hours.lunch)
    return start_m < l1 and l0 < end_m


def week_bounds(d: date) -> tuple[date, date]:
    """Monday..Friday of the week containing d."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=4)


def calendar_text(clinic: Clinic, today: date, days: int = 21) -> str:
    """A plain-text calendar given to the LLM so it can resolve 'next Tuesday' etc. without guessing."""
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        tags = []
        if i == 0:
            tags.append("today")
        if is_holiday(clinic, d):
            tags.append("clinic holiday - closed")
        elif not is_open_day(clinic, d):
            tags.append("weekend - closed")
        for p in clinic.providers:
            if p.on_leave(d):
                tags.append(f"{p.id} on leave")
        lines.append(f"{d.isoformat()} {d.strftime('%A')}" + (f" ({'; '.join(tags)})" if tags else ""))
    return "\n".join(lines)


def local_now(received_at: datetime) -> tuple[date, int]:
    """Date and minute-of-day of a message timestamp (already clinic-local in the data)."""
    return received_at.date(), received_at.hour * 60 + received_at.minute
