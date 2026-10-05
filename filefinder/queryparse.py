"""Turn what you typed into words plus filters, using plain rules.

"large videos"           type video, size over 50 MB
"PDFs in Downloads"      type PDF, folder named Downloads
"PDF I downloaded last month"
                         type PDF, folder Downloads, modified last calendar month
"tax 2025"               just the words tax and 2025
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .textutil import fold, split_words

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3
LARGE, SMALL = 50 * MB, 100 * KB
_UNITS = {"k": KB, "kb": KB, "m": MB, "mb": MB, "g": GB, "gb": GB}
_NUMBER = r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|k|m|g)\b"

TYPE_WORDS = {
    "pdf": "pdf", "pdfs": "pdf",
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "image": "image", "images": "image", "screenshot": "image", "screenshots": "image",
    "video": "video", "videos": "video", "movie": "video", "movies": "video",
    "music": "audio", "song": "audio", "songs": "audio", "audio": "audio",
    "document": "document", "documents": "document", "doc": "document", "docs": "document",
    "spreadsheet": "spreadsheet", "spreadsheets": "spreadsheet",
    "presentation": "presentation", "presentations": "presentation",
    "slides": "presentation", "archive": "archive", "archives": "archive",
    "folder": "folder", "folders": "folder", "directory": "folder", "directories": "folder",
    "code": "code", "script": "code", "scripts": "code",
}

# Words that carry no meaning for a file search.
STOP_WORDS = {
    "a", "an", "the", "i", "my", "me", "of", "from", "in", "on", "at", "to", "for", "with",
    "and", "or", "that", "this", "file", "files", "named", "called", "find", "show",
    "where", "is", "was", "were", "had", "got", "have", "ve", "it", "some", "all",
}

@dataclass
class ParsedQuery:
    terms: list = field(default_factory=list)         # words to look for
    kinds: list = field(default_factory=list)         # file types, for example pdf
    min_size: int | None = None
    max_size: int | None = None
    after: float | None = None                        # dated on or after
    before: float | None = None                       # modified before
    folder_words: list = field(default_factory=list)  # folder names to look inside
    notes: list = field(default_factory=list)         # plain language summary

    def has_filters(self) -> bool:
        return bool(self.kinds or self.folder_words or self.min_size is not None
                    or self.max_size is not None or self.after is not None
                    or self.before is not None)

def _cut(text: str, pattern: str):
    """Remove the first match of pattern from text. Returns (match, remaining text)."""
    m = re.search(pattern, text)
    if not m:
        return None, text
    return m, text[:m.start()] + " " + text[m.end():]

def _bytes(m) -> int:
    return int(float(m.group(1)) * _UNITS[m.group(2)])

def _date_rule(text: str, now: datetime):
    """Find a phrase like 'last month'. Returns (match, after, before, label) or None."""
    today = datetime(now.year, now.month, now.day)
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    previous_first = (first - timedelta(days=1)).replace(day=1)
    year_start = today.replace(month=1, day=1)
    previous_year = year_start.replace(year=year_start.year - 1)

    m = re.search(r"\b(?:past|last|previous)\s+(\d+)\s+(day|week|month|year)s?\b", text)
    if m:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
        n = int(m.group(1))
        return m, now - timedelta(days=n * days), None, f"in the last {n} {m.group(2)}s"

    rules = [
        (r"\btoday\b", today, today + timedelta(days=1), "today"),
        (r"\byesterday\b", today - timedelta(days=1), today, "yesterday"),
        (r"\bthis week\b", monday, None, "this week"),
        (r"\blast week\b", monday - timedelta(days=7), monday, "last week"),
        (r"\bthis month\b", first, None, "this month"),
        (r"\blast month\b", previous_first, first, "last month"),
        (r"\bthis year\b", year_start, None, "this year"),
        (r"\blast year\b", previous_year, year_start, "last year"),
        (r"\brecent(?:ly)?\b", now - timedelta(days=14), None, "in the last 14 days"),
    ]
    for pattern, after, before, label in rules:
        m = re.search(pattern, text)
        if m:
            return m, after, before, label
    return None

def _range_text(after: datetime, before: datetime | None) -> str:
    if before is None:
        return f"since {after:%Y-%m-%d}"
    return f"{after:%Y-%m-%d} to {before - timedelta(days=1):%Y-%m-%d}"

def parse_query(text: str, now: float | None = None) -> ParsedQuery:
    q = ParsedQuery()
    now_dt = datetime.fromtimestamp(now) if now else datetime.now()
    s = " " + fold(text).lower() + " "

    # Sizes: "over 10 MB", "under 500kb", "large", "small"
    m, s = _cut(s, r"(?:\b(?:over|bigger than|larger than|more than|above|at least)|>)\s*" + _NUMBER)
    if m:
        q.min_size = _bytes(m)
        q.notes.append(f"size over {m.group(1)} {m.group(2).upper()}")
    m, s = _cut(s, r"(?:\b(?:under|smaller than|less than|below|at most)|<)\s*" + _NUMBER)
    if m:
        q.max_size = _bytes(m)
        q.notes.append(f"size under {m.group(1)} {m.group(2).upper()}")
    if q.min_size is None:
        m, s = _cut(s, r"\b(?:large|big|huge|massive)\b")
        if m:
            q.min_size = LARGE
            q.notes.append("large (over 50 MB)")
    if q.max_size is None:
        m, s = _cut(s, r"\b(?:small|tiny)\b")
        if m:
            q.max_size = SMALL
            q.notes.append("small (under 100 KB)")

    # Dates, based on when the file was last modified
    found = _date_rule(s, now_dt)
    if found:
        m, after, before, label = found
        s = s[:m.start()] + " " + s[m.end():]
        q.after = after.timestamp()
        q.before = before.timestamp() if before else None
        q.notes.append(f"dated {label} ({_range_text(after, before)})")

    # Places: "in Downloads", "PDF I downloaded"
    while True:
        m, s = _cut(s, r"\bin\s+(?:my\s+|the\s+)?([a-z0-9]+)\b")
        if not m:
            break
        q.folder_words.append(m.group(1))
    m, s = _cut(s, r"\bdownloaded\b")
    if m:
        q.folder_words.append("downloads")

    # What is left: type words, filler words, and the real search words
    for word in split_words(s):
        if word in TYPE_WORDS:
            if TYPE_WORDS[word] not in q.kinds:
                q.kinds.append(TYPE_WORDS[word])
        elif word not in STOP_WORDS and word not in q.terms:
            q.terms.append(word)
    if q.kinds:
        q.notes.insert(0, "type: " + ", ".join(q.kinds))
    return q
