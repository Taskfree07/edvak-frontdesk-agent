"""Rules-mode date phrases ("Friday afternoon", "next week", "Wednesday the 18th", "this Thursday at 2pm").

Used only by the rules baseline. In LLM mode Claude resolves dates against the supplied calendar.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from frontdesk.data import Clinic
from frontdesk.policy.calendar import next_business_days, week_bounds

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3, "viernes": 4}


def _next_weekday(today: date, wd: int, next_week: bool = False) -> date:
    if next_week:
        monday = week_bounds(today)[0] + timedelta(days=7)
        return monday + timedelta(days=wd)
    delta = (wd - today.weekday()) % 7
    return today + timedelta(days=delta)


def _day_number(today: date, n: int) -> date | None:
    for i in range(0, 45):
        d = today + timedelta(days=i)
        if d.day == n:
            return d
    return None


def parse_time(text: str) -> str | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", text, re.I)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = m.group(3).lower().replace(".", "")
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def parse_part_of_day(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(morning|am\b|por la mañana)", t):
        return "morning"
    if re.search(r"\b(afternoon|after lunch|por la tarde)", t):
        return "afternoon"
    return "any"


def parse_point(text: str, today: date, clinic: Clinic, anchor: date | None = None) -> tuple[date, date] | None:
    """A date window mentioned in `text`, or None."""
    t = text.lower()
    m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b(?:,)?\s+(?:the\s+)?(\d{1,2})"
                  r"(?:st|nd|rd|th)?\b", t)
    if m:
        d = _day_number(today, int(m.group(2)))
        if d:
            return d, d
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        d = _day_number(today, int(m.group(1)))
        if d:
            return d, d
    if re.search(r"\b(today or tomorrow|hoy o mañana)\b", t):
        days = next_business_days(clinic, today, 2)
        return days[0], days[-1]
    if re.search(r"\b(today|hoy)\b", t):
        return today, today
    if re.search(r"\btomorrow\b", t):
        d = next_business_days(clinic, today, 1, include_start=False)[0]
        return today + timedelta(days=1), d
    if re.search(r"\bfollowing week\b", t) and anchor:
        mon = week_bounds(anchor)[0] + timedelta(days=7)
        return mon, mon + timedelta(days=4)
    nxt = bool(re.search(r"\b(next week|la próxima semana|la proxima semana|semana que viene)\b", t))
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            d = _next_weekday(today, wd, next_week=nxt)
            return d, d
    if nxt:
        mon = week_bounds(today)[0] + timedelta(days=7)
        return mon, mon + timedelta(days=4)
    if re.search(r"\b(this week|esta semana)\b", t):
        return today, week_bounds(today)[1]
    return None
