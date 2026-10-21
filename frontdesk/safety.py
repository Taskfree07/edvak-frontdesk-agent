"""Safety gate (clinic.json -> safety_policy).

Part A (before any LLM call): red-flag patterns. Every match is checked in its own clause:
- clear, current wording ("I can't breathe")                        -> emergency now, final, no AI needed
- negated / historical / informational / quoted wording             -> deferred: the AI decides
  ("no chest pain", "history of stroke", "signs of a stroke?", a quoted "I can't breathe")
- inside a figure of speech ("dying to get in", "gave me a heart attack") -> deferred: the AI decides
Part B (after the LLM understands the message): the LLM's clinical-concern level is OR-ed in. A deferred hit is
cleared ONLY by an explicit LLM all-clear; if the LLM is unavailable, times out or fails, it escalates.

Outcomes: emergency | urgent_clinical | none.

Limits: written by an engineer from public red-flag guidance (heart attack and stroke warning signs, anaphylaxis,
hypoglycaemia, suicide-risk language), English plus common Spanish phrases, tested on synthetic messages only. Not
clinically reviewed and not exhaustive; the LLM is the second line, and humans the third.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Level = Literal["emergency", "urgent_clinical", "none"]

_THINNER = r"(warfarin|coumadin|jantoven|blood ?thinners?|eliquis|apixaban|xarelto|rivaroxaban|anticoagulant\w*)"
_PILLS = r"(pills?|tablets?|meds|medications?|medicines?)"

# (category, pattern). Broad on purpose: false positives cost a phone call, misses can cost a life.
EMERGENCY_PATTERNS: list[tuple[str, str]] = [
    ("heart attack", r"\b(heart|hart|harte)\s*-?\s*at+ac?k\w*\b|\bcardiac arrest\b|\binfarto\b"
                     r"|\bataque (al|del) coraz[oó]n\b"),
    ("chest pain", r"\bchest\b.{0,40}\b(pain|pian|pains|pressure|tight\w*|crushing|squeez\w*|heavy|heaviness|hurts?"
                   r"|hurting|aching)\b|\b(crushing|squeezing|tight|heavy)\b.{0,20}\bchest\b"
                   r"|\b(pain|pressure|tightness|heaviness|squeezing)\b.{0,15}\b(in|on|across) (my|the|his|her) "
                   r"chest\b"
                   r"|\b(pain|pressure)\b.{0,40}\b(left arm|jaw)\b|\bdolor\b.{0,25}\bpecho\b"
                   r"|\bpecho\b.{0,25}\b(dolor|apret\w*|presi[oó]n)\b"),
    ("breathing", r"\b(can'?t|cannot|can ?not|unable to)\s+(breathe?|breath\w*|catch my breath|get (enough )?air"
                  r"|finish (a |my )?sentences?|talk)\b|\b(not|isn'?t|stopped|stop|no longer) breathing\b"
                  r"|\b(struggling|hard|difficult\w*|trouble|gasping)\s+(to\s+|for\s+)?(breath\w*|air)\b"
                  r"|\bshort(ness)? of breath\b|\blips?\b.{0,15}\b(blue|grey|gray|purple)\b|\bchok(ing|ed|es)\b"
                  r"|\bno puedo respirar\b|\bme ahogo\b|\bme falta el aire\b"),
    ("stroke", r"\b(face|smile|mouth)\b.{0,20}\b(droop\w*|drooping|crooked|lopsided)\b|\bslurr\w*\b"
               r"|\bsudden(ly)?\b.{0,40}\b(numb\w*|weak\w*|can'?t (speak|see|move|walk)|confus\w*|vision|blind\w*)\b"
               r"|\b(loss of|lost|losing)\s+(my\s+)?(vision|sight)\b|\bstroke\b|\bderrame cerebral\b"
               r"|\bboca torcida\b"),
    ("head injury", r"\b(hit|bump\w*|bang\w*|struck|injur\w*)\b.{0,20}\bhead\b.{0,80}\b(confus\w*|vomit\w*|"
                    r"pass\w* out|unconscious|drows\w*|not making sense|won'?t wake)"
                    r"|\b(fell|fall|fallen)\b.{0,80}\b(confus\w*|unconscious|won'?t wake|not making sense)"
                    r"|\b(golpe\w*|se cay[oó])\b.{0,40}\b(cabeza)\b.{0,60}\b(confundid\w*|vomit\w*)"),
    ("severe allergic reaction", r"\b(throat|tongue|lips?|face)\b.{0,20}\b(swell\w*|swollen|closing|tight\w*)\b"
                                 r"|\b(swollen|swelling)\b.{0,20}\b(face|lips?|tongue|throat)\b|\banaphyla\w*"
                                 r"|\bgarganta\b.{0,25}\b(cerr\w*|hinch\w*)|\b(cara|labios|lengua)\b.{0,15}\bhinchad\w*"),
    ("low blood sugar", r"\b(blood )?sugar\b.{0,30}\b([1-5]\d|6\d)\b(?!\s*(%|years?|yo|kg|lb))"
                        r"|\bglucose\b.{0,20}\b([1-5]\d|6\d)\b"
                        r"|\b(blood )?sugar\b.{0,25}\b(really |very |too |so |is )?(low|dropp\w*|crash\w*|tanking)\b"
                        r"|\b(glucose|sugar|meter|reading)\b.{0,20}\b(?-i:LO)\b"
                        r"|\bhypo(glycemi\w*|glycaemi\w*)?\b.{0,40}\b(shak\w*|sweat\w*|confus\w*|dizzy)"
                        r"|\bdiabetic\b.{0,60}\b(shak\w*|sweat\w*)\b.{0,40}\b(confus\w*|dizzy|faint\w*|sweat\w*)"
                        r"|\baz[uú]car\b.{0,25}\bbaj\w*"),
    ("seizure", r"\b(seiz\w*|siez\w*|seaz\w*)\b|\bconvuls\w*|\bconvulsi\w*|\bhaving a fit\b"),
    ("unresponsive", r"\bunresponsive\b|\b(won'?t|will not|can'?t|doesn'?t|isn'?t|not)\s+(wake|waking|respond\w*)"
                     r"|\bpassed out\b|\bunconscious\b|\bfainted\b|\bcollapsed\b|\bdesmay\w*|\binconsciente\b"
                     r"|\bno (despierta|responde)\b"),
    ("overdose / poisoning", rf"\boverdos\w*|\bsobredosis\b"
                             rf"|\btook\b.{{0,20}}\b(too many|a lot of|a bunch of|a handful of|the whole bottle|"
                             rf"all (of )?(my|the|his|her))\b.{{0,25}}\b{_PILLS}"
                             rf"|\b(toddler|child|kid|baby|son|daughter)\b.{{0,40}}\b(swallowed|ate|got into)\b"
                             rf".{{0,30}}\b({_PILLS[1:-1]}|bleach|detergent|battery|chemicals?)\b"
                             rf"|\b(swallowed|drank|ate)\b.{{0,25}}\b(bleach|detergent|poison|antifreeze|battery)\b"
                             rf"|\bpoison control\b"),
    ("dangerous bleeding", rf"\b(black|dark|tarry|bloody)\s+(stools?|poop)|\b(vomit\w*|cough\w*|throw\w* up|"
                           rf"spitting)\b.{{0,15}}\bblood|\bbleed\w*\b.{{0,30}}\b(won'?t stop|not stopping|a lot|"
                           rf"heavily|lots|everywhere)\b|\bheavy bleeding\b"
                           rf"|\b{_THINNER}\b.{{0,80}}\bbleed\w*|\bbleed\w*\b.{{0,80}}\b{_THINNER}"
                           rf"|\bbleed\w*\b.{{0,30}}\bfor (an|over an|more than an|\d+) hours?\b"
                           rf"|\bpregnan\w*\b.{{0,40}}\bbleed\w*|\bbleed\w*\b.{{0,40}}\bpregnan\w*"
                           rf"|\bvomit\w* sangre\b|\bsangr\w*\b.{{0,20}}\b(mucho|no para|abundante)\b"),
    ("self-harm (explicit)", r"\b(kill|harm)\s+myself\b|\b(hang|shoot|stab|drown)\w*\s+myself\b"
                             r"|\b(want|wanna|wann|going|plan\w*|thinking about|thought about)\s+(to\s+)?"
                             r"(hurt|harm|kill|end)\w*\s+(myself|my life|it all)\b"
                             r"|\bsuicid\w*|\bend (my (own )?life|it all)\b|\btake my (own )?life\b"
                             r"|\b(want|wanna|wann) (to )?die\b|\bbetter off dead\b"
                             r"|\b(don'?t|do not) want to (live|be alive|wake up)\b"
                             r"|\bquitarme la vida\b|\bquiero (morir\w*|matarme|suicidarme)\b|\bmatarme\b"
                             r"|\bno quiero vivir\b"),
    ("severe headache / meningitis", r"\bworst (headache|pain) of my life\b|\bthunderclap\b"
                                     r"|\bstiff neck\b.{0,60}\bfever\b|\bfever\b.{0,60}\bstiff neck\b"),
    ("fever in a young baby", r"\b(\d+\s*-?\s*weeks?|[12]\s*-?\s*months?)\s*-?\s*old\b.{0,60}\bfever"
                              r"|\bnewborn\b.{0,40}\bfever"),
]

# Figures of speech that contain red-flag words. A red-flag match inside one is deferred to the AI.
IDIOM_PATTERNS: list[tuple[str, str]] = [
    ("idiom: dying to/for", r"\bdying (to|for)\b"),
    ("idiom: dying of", r"\bdying of (laughter|boredom|embarrassment|curiosity|thirst)\b"),
    ("idiom: killing me", r"\b(is|are|it'?s|was)\s+killing me\b"),
    ("idiom: to death", r"\b(bored|scared|worried|tired|sick) to death\b"),
    ("idiom: dead tired", r"\bdead (tired|beat)\b"),
    ("idiom: heart attack (figurative)", r"\b(gave|giving|give|nearly had|almost had|practically had)\b.{0,10}"
                                         r"\ba heart attack\b"),
    ("idiom: could die", r"\b(i )?could (just )?(die|kill myself)\b"),
]

# Same-day clinician attention, not an emergency: low mood / hopelessness without explicit intent, out of a
# critical medication.
URGENT_PATTERNS: list[tuple[str, str]] = [
    ("low mood / hopelessness", r"\b(feel\w*|felt)\b.{0,15}\b(low|down|depressed|hopeless|worthless)\b"
                                r"|\bbeen (so |really |very )?(depressed|hopeless)\b"
                                r"|\b(don'?t|do not) see the point\b|\bcan'?t go on\b|\bwant to give up\b"),
    ("out of critical medication", r"\b(out of|ran out of|no more)\b.{0,20}\b(insulin|anticoagulant|warfarin|"
                                   r"seizure|antidepressant)\b"),
]

# Context that turns a red-flag match into "not clearly happening now" (only looked for in the same clause,
# before the match).
_CLAUSE_BREAK = re.compile(r"[.!?;,\n]|\bbut\b|\band\b|\bpero\b|\by\b", re.I)
_NEGATION = re.compile(r"\b(no|not|never|without|don'?t|doesn'?t|didn'?t|isn'?t|wasn'?t|aren'?t|haven'?t|hasn'?t|"
                       r"denies|deny|nor|none|sin|nunca|ni)\b", re.I)
_HISTORY = re.compile(r"\b(history of|had (a|an)|previous|prior|died (of|from)|passed away (of|from)|survived|"
                      r"recovered from|years ago|last (year|month)|in (19|20)\d\d|antecedentes de|tuve un)\b", re.I)
_RECENT_AFTER = re.compile(r"^\W{0,3}(this (morning|afternoon|evening)|today|tonight|just now|right now|earlier|"
                           r"last night|yesterday|an? (few )?(minutes?|hours?) ago|\d+ (minutes?|mins?|hours?) ago|"
                           r"esta (mañana|tarde|noche)|hoy|anoche)\b", re.I)
_HISTORY_AFTER = re.compile(r"^\W{0,3}(in (19|20)\d\d|\d+ (years?|months?) ago|years ago|last year|sometimes|"
                            r"at times|occasionally|usually|now and then)\b", re.I)
_INFO = re.compile(r"\b(signs? of|symptoms? of|what (is|are|does)|how (do|can) (i|you) (know|tell)|"
                   r"information (about|on)|info on|learn about|risk of|prevent\w*|asking for|school project|"
                   r"worried about getting)\b", re.I)
_QUOTED = re.compile(r"(?:^|[\s:(])[\"“'‘](.+?)[\"”'’](?=[\s.,!?;:)]|$)")


def _compile(patterns: list[tuple[str, str]]) -> list[tuple[str, re.Pattern[str]]]:
    """A pattern's "x.{0,N}y" gap never crosses a sentence end, so words from two sentences are not combined."""
    return [(cat, re.compile(pat.replace(".{", "[^.!?\\n]{"), re.IGNORECASE)) for cat, pat in patterns]


_EMERGENCY = _compile(EMERGENCY_PATTERNS)
_IDIOMS = _compile(IDIOM_PATTERNS)
_URGENT = _compile(URGENT_PATTERNS)


@dataclass
class SafetyResult:
    level: Level
    hits: list[str] = field(default_factory=list)          # emergency categories matched in clear wording
    idiom_hits: list[str] = field(default_factory=list)    # red-flag words inside figures of speech, held for the AI
    deferred_hits: list[str] = field(default_factory=list)  # negated / historical / informational / quoted, for the AI
    urgent_hits: list[str] = field(default_factory=list)
    llm_concern: str | None = None
    decided_by: str = "keywords"

    def as_dict(self) -> dict:
        return {"level": self.level, "hits": self.hits, "idiom_hits": self.idiom_hits,
                "deferred_hits": self.deferred_hits, "urgent_hits": self.urgent_hits,
                "llm_concern": self.llm_concern, "decided_by": self.decided_by}

    @property
    def pending(self) -> bool:
        return bool(self.idiom_hits or self.deferred_hits)


def _scan(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [cat for cat, pat in patterns if pat.search(text)]


def _qualifier(text: str, start: int, end: int, quoted: list[tuple[int, int]]) -> str | None:
    """Why this match is not clearly a current emergency, or None if it is."""
    if any(a <= start and end <= b for a, b in quoted):
        return "quoted"
    before = text[max(0, start - 60):start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    clause = before[breaks[-1].end():] if breaks else before
    # A negation only counts right before the phrase ("no chest pain", "not having trouble breathing"), so
    # "this pain doesn't stop, I want to hang myself" is not read as negated.
    if _NEGATION.search(" ".join(clause.split()[-3:])):
        return "negated"
    after = text[end:end + 30]
    if (_HISTORY.search(clause) and not _RECENT_AFTER.search(after)) or _HISTORY_AFTER.search(after):
        return "past / history"
    if _INFO.search(clause):
        return "information question"
    return None


def keyword_gate(body: str) -> SafetyResult:
    """Part A. Runs before the LLM. `emergency` here is final."""
    text = body.replace("’", "'").replace("‘", "'")
    idiom_spans = [(m.start(), m.end(), cat) for cat, pat in _IDIOMS for m in pat.finditer(text)]
    quoted = [(m.start(1), m.end(1)) for m in _QUOTED.finditer(text)]
    clear: list[str] = []
    deferred: list[str] = []
    idioms = sorted({cat for _, _, cat in idiom_spans})
    for cat, pat in _EMERGENCY:
        reasons = []
        for m in pat.finditer(text):
            if any(a < m.end() and m.start() < b for a, b, _ in idiom_spans):
                reasons.append("figure of speech")
                continue
            why = _qualifier(text, m.start(), m.end(), quoted)
            if why is None:
                clear.append(cat)
                break
            reasons.append(why)
        else:
            if reasons:
                deferred.append(f"{cat} ({reasons[0]})")
    urgent = _scan(text, _URGENT)
    if clear:
        return SafetyResult("emergency", hits=clear, idiom_hits=idioms, urgent_hits=urgent)
    if deferred or idioms:  # undecided: the AI decides, and it stays escalated if the AI is unavailable
        return SafetyResult("none", idiom_hits=idioms, deferred_hits=deferred, urgent_hits=urgent,
                            decided_by="pending_llm")
    if urgent:
        return SafetyResult("urgent_clinical", urgent_hits=urgent)
    return SafetyResult("none")


def combine(part_a: SafetyResult, llm_concern: str | None) -> SafetyResult:
    """Part B. llm_concern in {none, routine, urgent_same_day, emergency} or None if the LLM was unavailable."""
    r = SafetyResult(part_a.level, list(part_a.hits), list(part_a.idiom_hits), list(part_a.deferred_hits),
                     list(part_a.urgent_hits), llm_concern=llm_concern, decided_by=part_a.decided_by)
    if r.level == "emergency":
        return r
    if llm_concern == "emergency":
        r.level, r.decided_by = "emergency", "llm"
        return r
    if r.pending and llm_concern not in ("none", "routine", "urgent_same_day"):
        # A possible red flag without a confident AI all-clear: err toward escalation.
        r.level, r.decided_by = "emergency", "red_flag_without_ai_clearance"
        return r
    if r.level == "urgent_clinical" or part_a.urgent_hits or llm_concern == "urgent_same_day":
        r.level = "urgent_clinical"
        r.decided_by = "keywords" if part_a.urgent_hits else "llm"
        return r
    if r.pending:
        r.decided_by = "red_flag_cleared_by_llm"
    return r
