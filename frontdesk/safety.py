"""Safety gate (clinic.json -> safety_policy).

Part A (before any LLM call): red-flag patterns. A clear hit escalates immediately.
Part B (after the LLM understands the message): the LLM's clinical-concern level is OR-ed in. A keyword hit that is
only an idiom ("dying to get in") is held back and cleared ONLY if the LLM also says there is no emergency.

Outcomes: emergency | urgent_clinical | none. The default when unsure is to escalate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Level = Literal["emergency", "urgent_clinical", "none"]

# (category, pattern). Patterns are deliberately broad; false positives cost a phone call, misses can cost a life.
EMERGENCY_PATTERNS: list[tuple[str, str]] = [
    ("chest pain", r"\b(chest|left arm|jaw)\b.{0,40}\b(pain|pressure|tight(ness)?|crushing|squeez\w*)\b"
                   r"|\b(crushing|squeezing)\b.{0,20}\bchest\b"),
    ("breathing", r"can'?t\s+(breathe|catch my breath|finish (a |my )?sentences?|talk)"
                  r"|\b(struggling|hard|difficult\w*|trouble)\s+(to\s+)?breath\w*|short(ness)? of breath"
                  r"|\blips?\b.{0,15}\b(blue|grey|gray)\b|\bgasping\b|no puedo respirar"),
    ("stroke", r"\b(face|smile)\b.{0,20}\b(droop\w*)\b|\bslurred\b|\bsudden(ly)?\b.{0,30}\b(numb\w*|weak\w*|"
               r"can'?t (speak|see|move))|\bstroke\b"),
    ("head injury", r"\b(hit|bump\w*|bang\w*|struck|injur\w*)\b.{0,20}\bhead\b.{0,80}\b(confus\w*|vomit\w*|"
                    r"pass\w* out|unconscious|drows\w*|not making sense|won'?t wake)"
                    r"|\b(fell|fall|fallen)\b.{0,80}\b(confus\w*|unconscious|won'?t wake|not making sense)"),
    ("bleeding", r"\b(black|dark|tarry|bloody)\s+(stools?|poop)|\b(vomit\w*|cough\w*|throw\w* up)\b.{0,15}\bblood"
                 r"|\bbleed\w*\b.{0,30}\b(won'?t stop|a lot|heavily|lots)\b|\bheavy bleeding\b"),
    ("low glucose", r"\b(blood )?sugar\b.{0,30}\b([1-5]\d|6\d)\b(?!\s*(%|years?|yo|kg|lb))"
                    r"|\bglucose\b.{0,20}\b([1-5]\d|6\d)\b|\bhypo(glycemi\w*|glycaemi\w*)?\b.{0,40}"
                    r"\b(shak\w*|sweat\w*|confus\w*|dizzy)"),
    ("anaphylaxis", r"\b(throat|tongue|lips?)\b.{0,20}\b(swell\w*|closing|tight\w*)|\banaphyla\w*"),
    ("self-harm (explicit)", r"\b(kill|hurt|harm)\s+myself\b|\bsuicid\w*|\bend (my|it all)\b|\btake my (own )?life\b"
                             r"|\bwant to die\b|\bbetter off dead\b|\boverdos\w*|\btook (all|too many) (my |of my )?"
                             r"(pills|tablets)\b|\bquitarme la vida\b"),
    ("seizure", r"\bseiz\w*\b|\bconvuls\w*"),
    ("unresponsive", r"\bunresponsive\b|\bwon'?t wake (up)?\b|\bpassed out\b|\bunconscious\b|\bfainted\b"),
    ("severe pain", r"\bworst (headache|pain) of my life\b|\bthunderclap\b"),
]

# Phrases that sound alarming but are usually figures of speech. A hit here is NOT escalated by itself; it is
# escalated unless the LLM independently judges the message non-urgent.
IDIOM_PATTERNS: list[tuple[str, str]] = [
    ("idiom: dying to", r"\bdying to\b"),
    ("idiom: killing me", r"\b(is|are|it'?s)\s+killing me\b"),
    ("idiom: dead tired", r"\bdead (tired|beat)\b"),
    ("idiom: heart attack (figurative)", r"\b(gave|giving) me a heart attack\b"),
]

# Same-day clinician attention, not an emergency: low mood / hopelessness without explicit intent, new worrying
# symptoms on high-risk medication. Routine handling continues alongside.
URGENT_PATTERNS: list[tuple[str, str]] = [
    ("low mood / hopelessness", r"\b(feel\w*|felt)\b.{0,15}\b(low|down|depressed|hopeless|worthless)\b"
                                r"|\bbeen (so |really |very )?(depressed|hopeless)\b"
                                r"|\b(don'?t|do not) see the point\b|\bcan'?t go on\b|\bwant to give up\b"),
    ("out of critical medication", r"\b(out of|ran out of|no more)\b.{0,20}\b(insulin|anticoagulant|warfarin|"
                                   r"seizure|antidepressant)\b"),
]


@dataclass
class SafetyResult:
    level: Level
    hits: list[str] = field(default_factory=list)        # emergency categories matched
    idiom_hits: list[str] = field(default_factory=list)  # held back pending the LLM
    urgent_hits: list[str] = field(default_factory=list)
    llm_concern: str | None = None
    decided_by: str = "keywords"

    def as_dict(self) -> dict:
        return {"level": self.level, "hits": self.hits, "idiom_hits": self.idiom_hits,
                "urgent_hits": self.urgent_hits, "llm_concern": self.llm_concern, "decided_by": self.decided_by}


def _scan(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    return [cat for cat, pat in patterns if re.search(pat, text, re.IGNORECASE)]


def keyword_gate(body: str) -> SafetyResult:
    """Part A. Runs before the LLM. `emergency` here is final."""
    text = body.replace("’", "'")
    idioms = _scan(text, IDIOM_PATTERNS)
    hits = _scan(text, EMERGENCY_PATTERNS)
    urgent = _scan(text, URGENT_PATTERNS)
    if hits:
        return SafetyResult("emergency", hits=hits, idiom_hits=idioms, urgent_hits=urgent)
    if idioms:  # undecided: an idiom alone waits for the LLM, and stays escalated if the LLM is unavailable
        return SafetyResult("none", idiom_hits=idioms, urgent_hits=urgent, decided_by="pending_llm")
    if urgent:
        return SafetyResult("urgent_clinical", urgent_hits=urgent)
    return SafetyResult("none")


def combine(part_a: SafetyResult, llm_concern: str | None) -> SafetyResult:
    """Part B. llm_concern in {none, routine, urgent_same_day, emergency} or None if the LLM was unavailable."""
    r = SafetyResult(part_a.level, list(part_a.hits), list(part_a.idiom_hits), list(part_a.urgent_hits),
                     llm_concern=llm_concern, decided_by=part_a.decided_by)
    if r.level == "emergency":
        return r
    if llm_concern == "emergency":
        r.level, r.decided_by = "emergency", "llm"
        return r
    if r.idiom_hits and llm_concern not in ("none", "routine", "urgent_same_day"):
        # Idiom without a confident LLM all-clear: err toward escalation.
        r.level, r.decided_by = "emergency", "idiom_without_llm_clearance"
        return r
    if r.level == "urgent_clinical" or llm_concern == "urgent_same_day":
        r.level = "urgent_clinical"
        r.decided_by = "keywords" if part_a.urgent_hits else "llm"
        return r
    if r.idiom_hits:
        r.decided_by = "idiom_cleared_by_llm"
    return r
