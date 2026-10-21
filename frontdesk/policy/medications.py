"""Match the medication a patient names ("Xanax", "metformina", "my inhaler") to their active list."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from frontdesk.data import Medication

# General drug-name knowledge (brand and Spanish names -> generic). Not derived from the sample messages.
ALIASES: dict[str, str] = {
    # brands
    "xanax": "alprazolam", "ambien": "zolpidem", "adderall": "amphetamine", "oxycontin": "oxycodone",
    "percocet": "oxycodone", "roxicodone": "oxycodone", "glucophage": "metformin", "zoloft": "sertraline",
    "lexapro": "escitalopram", "prilosec": "omeprazole", "synthroid": "levothyroxine", "levoxyl": "levothyroxine",
    "lipitor": "atorvastatin", "norvasc": "amlodipine", "zestril": "lisinopril", "prinivil": "lisinopril",
    "zestoretic": "lisinopril-hctz", "aricept": "donepezil", "coumadin": "warfarin", "jantoven": "warfarin",
    "lantus": "insulin", "basaglar": "insulin", "toujeo": "insulin", "zyrtec": "cetirizine",
    "proair": "albuterol", "ventolin": "albuterol", "proventil": "albuterol", "inhaler": "albuterol",
    "valium": "diazepam", "klonopin": "clonazepam", "ativan": "lorazepam", "vicodin": "hydrocodone",
    # Spanish
    "metformina": "metformin", "sertralina": "sertraline", "omeprazol": "omeprazole",
    "levotiroxina": "levothyroxine", "atorvastatina": "atorvastatin", "amlodipino": "amlodipine",
    "insulina": "insulin", "warfarina": "warfarin", "escitalopram": "escitalopram", "donepezilo": "donepezil",
    "inhalador": "albuterol", "cetirizina": "cetirizine", "alprazolam": "alprazolam", "zolpidem": "zolpidem",
    "oxicodona": "oxycodone",
}


def _words(s: str) -> list[str]:
    return re.findall(r"[a-záéíóúñ]+(?:-[a-z]+)?", s.lower())


def canonical(name: str) -> str:
    """'Xanax' -> 'alprazolam'; 'Metformin 500 mg' -> 'metformin'; 'Lisinopril-HCTZ 20/12.5' -> 'lisinopril-hctz'."""
    for w in _words(name):
        if w in ALIASES:
            return ALIASES[w]
        if w not in {"mg", "mcg", "my", "the", "refill", "of", "pen", "units", "ml", "hfa", "please"}:
            return w
    return name.strip().lower()


def match_medication(query: str, meds: list[Medication]) -> Medication | None:
    """Best active medication for a free-text name, or None. Exact generic first, then prefix, then fuzzy."""
    q = canonical(query)
    if not q:
        return None
    by_key = {canonical(m.medication): m for m in meds}
    if q in by_key:
        return by_key[q]
    for key, m in by_key.items():  # 'lisinopril' should not silently match 'lisinopril-hctz' unless unique
        if key.split("-")[0] == q or key.startswith(q):
            candidates = [k for k in by_key if k.split("-")[0] == q or k.startswith(q)]
            if len(candidates) == 1:
                return m
    best, score = None, 0.0
    for key, m in by_key.items():
        r = SequenceMatcher(None, q, key).ratio()
        if r > score:
            best, score = m, r
    return best if score >= 0.85 else None
