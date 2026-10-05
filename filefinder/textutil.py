"""Small text helpers shared by the indexer and the search code."""
from __future__ import annotations

import re
import unicodedata

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[\W_]+")

# Letters that are not "a letter plus an accent", so they need their own plain spelling.
_PLAIN_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "TH", "ı": "i",
})

def fold(text: str) -> str:
    """Remove accents so that 'cafe' and 'café', 'tromso' and 'Tromsø' look the same."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_PLAIN_LETTERS))
    return "".join(c for c in decomposed if not unicodedata.combining(c))

def split_words(text: str) -> list[str]:
    """Cut a file name or a sentence into lowercase words.

    TaxReturn_2025.pdf gives: tax, return, 2025, pdf.
    """
    text = _CAMEL.sub(" ", fold(text))
    return _SEPARATORS.sub(" ", text).lower().split()

def trigrams(word: str) -> set[str]:
    """Overlapping 3 letter pieces: 'invoice' gives inv, nvo, voi, oic, ice."""
    return {word[i:i + 3] for i in range(len(word) - 2)}

def edit_distance(a: str, b: str, limit: int = 3) -> int:
    """How many single letter edits turn a into b.

    Counts insert, delete, replace and swapping two neighbouring letters
    (the most common typing slip). Gives up early and returns limit + 1
    once the answer is certain to be larger than limit.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    older = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            best = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                best = min(best, older[j - 2] + 1)
            current[j] = best
        if min(current) > limit:
            return limit + 1
        older, previous = previous, current
    return previous[-1] if previous[-1] <= limit else limit + 1
