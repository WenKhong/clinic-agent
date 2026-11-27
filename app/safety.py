"""Minimal emergency keyword check, run before anything else in /ask.

This is a stopgap, not the safety layer in SPEC §9.2. It covers only a few
high-precision Tier A phrases (SF-01) in English and BM. It does not have the
full normalisation (SMS spellings, repeated letters), the Chinese list, the
Tier B words, or the LLM safety classifier (SF-02). Missing an emergency is
the worst failure, so this check runs before search and the LLM.
"""

import re

# Fixed text, word for word from SPEC Appendix A (emergency_general).
EMERGENCY_REPLY = (
    "This may be an emergency. Call 999 now (or 112 from a mobile), or go to the "
    "nearest hospital emergency department. Don't wait for a reply here. Our clinic "
    "team will see this message, but they cannot send help."
)

# Tier A phrases from SPEC SF-01 (EN + BM). Ambiguous words such as "sakit hati"
# (BM idiom for being upset) are Tier B and deliberately left out.
TIER_A_PHRASES = [
    "chest pain",
    "can't breathe",
    "cannot breathe",
    "can not breathe",
    "unconscious",
    "kill myself",
    "stroke",
    "overdose",
    "sakit dada",
    "tak boleh bernafas",
    "tidak boleh bernafas",
    "pengsan",
    "bunuh diri",
    "angin ahmar",
    "sawan",
]


def normalise(text: str) -> str:
    """Casefold, drop apostrophes, turn other punctuation into spaces, collapse spaces."""
    text = text.casefold().replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


_PHRASES = [normalise(p) for p in TIER_A_PHRASES]


def is_emergency(text: str) -> bool:
    """True if the text contains a Tier A phrase as whole words."""
    padded = f" {normalise(text)} "
    return any(f" {phrase} " in padded for phrase in _PHRASES)
