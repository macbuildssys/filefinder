"""Search: parse, expand words, find candidates, score, explain.

Pipeline for each search
  1. queryparse turns your text into words and filters.
  2. Each word is widened: itself, words starting with it, close spellings
     (typos) and related words learned from your files.
  3. SQLite FTS5 finds files matching the widened words and ranks them with BM25.
  4. We rescore with how good each match was, your past clicks and recency,
     and write a plain language reason for every result.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .config import HOME
from .db import path_ranges
from .extract import type_label
from .queryparse import ParsedQuery, parse_query
from .textutil import edit_distance, fold, split_words, trigrams

MAX_TERMS = 8
FEW_RESULTS = 8          # below this many hits we try harder (typos, OR matching)
CLICK_HALF_LIFE_DAYS = 60
HOME_BOOST = 2.0         # your own files rank above system files with an equal match
CONTENT_LOOKUPS = 40     # how many results get the (slower) 'which words matched in the text' check
SQL_RANK = "bm25(docs, 10.0, 3.0, 1.0, 3.0)"   # name 10x, folder 3x, text 1x, photo details 3x

@dataclass
class Hit:
    id: int
    path: str
    name: str
    folder: str
    kind: str
    label: str
    size: int
    mtime: float
    taken: float | None = None     # date a photo was taken
    score: float = 0.0
    reason: str = ""

@dataclass
class SearchResult:
    hits: list = field(default_factory=list)
    terms: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    did_you_mean: str = ""
    suggestions: list = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""

@dataclass
class Group:
    """One search word together with everything it may also match."""
    term: str
    known: bool
    fuzzy: list = field(default_factory=list)     # close spellings
    related: list = field(default_factory=list)   # words that go with it

FILE_COLUMNS = "f.id, f.path, f.name, f.folder, f.kind, f.ext, f.size, f.mtime, f.taken"
N_FILE = 9    # number of columns above
DATED = "COALESCE(f.taken, f.mtime)"   # a photo counts by when it was taken, other files by last change

def _hit(row) -> Hit:
    id_, path, name, folder, kind, ext, size, mtime, taken = row[:N_FILE]
    return Hit(id_, path, name, folder, kind, type_label(kind, ext), size, mtime, taken)

def is_known(conn, word: str) -> bool:
    return word.isdigit() or conn.execute(
        "SELECT 1 FROM vocab WHERE word = ?", (word,)).fetchone() is not None

def fuzzy_words(conn, term: str, limit: int = 4) -> list[str]:
    """Words in your file names that are one or two typos away from term.

    Step 1 (fast): the trigram index lists words sharing 3 letter pieces.
    Step 2 (precise): edit distance keeps only the really close ones.
    """
    if len(term) < 4 or term.isdigit():
        return []
    allowed = 1 if len(term) <= 5 else 2
    expr = " OR ".join(f'"{t}"' for t in sorted(trigrams(term)))
    try:
        rows = conn.execute(
            "SELECT v.word, v.src FROM vocab_tri JOIN vocab v ON v.id = vocab_tri.rowid "
            "WHERE vocab_tri MATCH ? ORDER BY rank LIMIT 300", (expr,))
        scored = []
        for word, src in rows:
            d = edit_distance(term, word, allowed)
            if 0 < d <= allowed:
                # closest first, then words used in names before words only seen in texts
                scored.append((d, -src, abs(len(word) - len(term)), word))
    except sqlite3.OperationalError:
        return []
    return [w for *_, w in sorted(scored)[:limit]]

def related_words(conn, term: str, limit: int = 3, min_score: float = 0.25) -> list[str]:
    rows = conn.execute(
        "SELECT other FROM related WHERE word = ? AND score >= ? ORDER BY score DESC LIMIT ?",
        (term, min_score, limit))
    return [r[0] for r in rows]

def build_groups(conn, terms, fuzzy_for_all: bool) -> list[Group]:
    groups = []
    for t in terms:
        known = is_known(conn, t)
        fuzzy = fuzzy_words(conn, t) if (fuzzy_for_all or not known) else []
        groups.append(Group(t, known, fuzzy, related_words(conn, t)))
    return groups

def _group_expr(g: Group) -> str:
    # Terms only contain letters and digits, so quoting them is always safe.
    parts = [f'"{g.term}"']
    if len(g.term) >= 2:
        parts.append(f'"{g.term}"*')              # also words that start with it
    parts += [f'"{w}"' for w in g.fuzzy + g.related]
    return "(" + " OR ".join(parts) + ")"

def _filters(q: ParsedQuery, folders: list[str], scope=None):
    """SQL conditions for the filters in the query. scope limits results to some folders."""
    sql, params = [], []
    if scope:
        inside, scope_params = path_ranges("f.path", scope)
        if inside:
            sql.append(inside)
            params += scope_params
    if q.kinds:
        sql.append("f.kind IN (%s)" % ",".join("?" * len(q.kinds)))
        params += q.kinds
    if q.min_size is not None:
        sql.append("f.size >= ?")
        params.append(q.min_size)
    if q.max_size is not None:
        sql.append("f.is_dir = 0 AND f.size <= ?")
        params.append(q.max_size)
    if q.after is not None:
        sql.append(f"{DATED} >= ?")
        params.append(q.after)
    if q.before is not None:
        sql.append(f"{DATED} < ?")
        params.append(q.before)
    for w in folders:   # w only has letters and digits
        sql.append("(lower(f.folder) LIKE ? OR lower(f.folder) LIKE ?)")
        params += [f"%/{w}", f"%/{w}/%"]
    return "".join(" AND " + s for s in sql), params

def _folder_names_that_exist(conn, words):
    """'in Downloads' only filters when a folder with that name exists."""
    real, leftover = [], []
    for w in words:
        row = conn.execute(
            "SELECT 1 FROM files WHERE is_dir = 1 AND name = ? COLLATE NOCASE LIMIT 1", (w,)
        ).fetchone()
        (real if row else leftover).append(w)
    return real, leftover

def _fts_rows(conn, groups, joiner, where, params, limit):
    expr = f" {joiner} ".join(_group_expr(g) for g in groups)
    sql = (f"SELECT {FILE_COLUMNS}, docs.name, docs.folder, docs.extra, {SQL_RANK} AS raw_score "
           "FROM docs JOIN files f ON f.id = docs.rowid "
           f"WHERE docs MATCH ?{where} ORDER BY raw_score LIMIT ?")
    try:
        return conn.execute(sql, [expr] + params + [limit]).fetchall(), expr
    except sqlite3.OperationalError:
        return [], expr

def _content_words(conn, expr, ids) -> dict:
    """Which words matched inside the file text, for the top results only."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    sql = ("SELECT rowid, snippet(docs, 2, char(1), char(2), '', 64) FROM docs "
           f"WHERE docs MATCH ? AND rowid IN ({marks})")
    found = {}
    try:
        for rowid, snip in conn.execute(sql, [expr] + list(ids)):
            found[rowid] = set(re.findall("\x01(.*?)\x02", snip or "", flags=re.S))
    except sqlite3.OperationalError:
        pass
    return found

def _click_weights(conn, terms, now) -> dict:
    """How much you opened each file after searching these words (older clicks fade)."""
    if not terms:
        return {}
    rows = conn.execute(
        "SELECT path, weight, ts FROM term_clicks WHERE term IN (%s)" % ",".join("?" * len(terms)),
        list(terms))
    weights: dict[str, float] = {}
    for path, weight, ts in rows:
        age_days = max(0.0, (now - ts) / 86400)
        weights[path] = weights.get(path, 0.0) + weight * 0.5 ** (age_days / CLICK_HALF_LIFE_DAYS)
    return weights

def _explain(g: Group, name_words, folder_words, extra_words, content_words):
    """Say how one search word matched. Returns (reason, quality) or None."""
    t = g.term
    places = (("name", name_words), ("folder name", folder_words),
              ("photo place or date", extra_words))
    for label, words in places:
        if t in words:
            return f'{label} has "{t}"', 1.0
    for label, words in places:
        if len(t) >= 2:
            start = next((w for w in words if w.startswith(t)), None)
            if start:
                return f'{label} has "{start}" (starts with "{t}")', 0.9
    for label, words in places:
        for w in g.fuzzy:
            if w in words:
                return f'"{t}" looks like "{w}" in the {label} (typo)', 0.7
    for label, words in places:
        for w in g.related:
            if w in words:
                return f'"{w}" often appears together with "{t}" in your files', 0.6
    for w in sorted(content_words):
        if w == t or w.startswith(t):
            return f'text inside the file mentions "{w}"', 0.8
        if w in g.fuzzy:
            return f'text inside the file has "{w}", close to "{t}"', 0.65
        if w in g.related:
            return f'text inside the file has "{w}", which goes with "{t}"', 0.55
    return None

def _score(conn, rows, groups, expr, terms, now, all_words_matched) -> list[Hit]:
    clicks = _click_weights(conn, terms, now)

    # First explain what we can from the file and folder names (free).
    prepared, need_text = [], []
    for row in rows:
        name_list = row[N_FILE].split()
        name_words, folder_words = set(name_list), set(row[N_FILE + 1].split())
        extra_words = set(row[N_FILE + 2].split())
        explained = [_explain(g, name_words, folder_words, extra_words, ()) for g in groups]
        if None in explained and len(need_text) < CONTENT_LOOKUPS:
            need_text.append(row[0])
        prepared.append((row, name_list, name_words, folder_words, extra_words, explained))

    # Only for the rest, ask SQLite which words matched inside the text (slower).
    content = _content_words(conn, expr, need_text)

    hits = []
    for row, name_list, name_words, folder_words, extra_words, explained in prepared:
        hit = _hit(row)
        reasons, qualities = [], []
        for g, found in zip(groups, explained):
            if found is None and hit.id in content:
                found = _explain(g, name_words, folder_words, extra_words, content[hit.id])
            if found is None and all_words_matched:
                found = (f'text inside the file matches "{g.term}"', 0.8)
            if found:
                reasons.append(found[0])
                qualities.append(found[1])
        quality = sum(qualities) / len(qualities) if qualities else 0.5
        coverage = max(len(qualities), 1) / len(groups)
        score = -row[N_FILE + 3] * quality * coverage    # BM25 is negative when good
        if name_list[:len(terms)] == terms:
            score += 2.0                                # name starts with your whole query
            reasons.append("name starts with your search")
        boost = 2.0 * math.log1p(clicks.get(hit.path, 0.0))
        if boost > 0.3:
            reasons.append("you opened this before after similar searches")
        when = hit.taken or hit.mtime
        if hit.path.startswith(HOME + "/"):
            score += HOME_BOOST
        score += boost + 0.5 * math.exp(-max(0.0, now - when) / (90 * 86400))
        hit.score = score
        hit.reason = "; ".join(reasons)
        hits.append(hit)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits

def _filter_only(conn, q, folders, limit, scope=None) -> list[Hit]:
    where, params = _filters(q, folders, scope)
    rows = conn.execute(
        f"SELECT {FILE_COLUMNS} FROM files f WHERE 1 = 1{where} ORDER BY {DATED} DESC LIMIT ?",
        params + [limit]).fetchall()
    why = "matches your filters: " + ", ".join(q.notes + [f"in {w}" for w in folders])
    hits = [_hit(r) for r in rows]
    for h in hits:
        h.reason = why
    return hits

def _corrected_text(text: str, groups) -> str:
    """Rewrite the typed text using the best spelling for words we do not know."""
    new = text
    for g in groups:
        if not g.known and g.fuzzy:
            new = re.sub(rf"\b{re.escape(g.term)}\b", g.fuzzy[0], new, flags=re.I)
    return new if new != text else ""

def run_search(conn, text: str, limit: int = 300, now: float | None = None,
               scope: list | None = None) -> SearchResult:
    """Search. scope is an optional list of folders: only files inside them are returned."""
    now = now or time.time()
    started = time.perf_counter()
    q = parse_query(text, now)
    folders, leftover = _folder_names_that_exist(conn, q.folder_words)
    terms = (q.terms + [w for w in leftover if w not in q.terms])[:MAX_TERMS]
    result = SearchResult(terms=terms, notes=q.notes + [f"in folder {w}" for w in folders])
    where, params = _filters(q, folders, scope)

    if not terms:
        if q.has_filters():
            result.hits = _filter_only(conn, q, folders, limit, scope)
    else:
        groups = build_groups(conn, terms, fuzzy_for_all=False)
        rows, expr = _fts_rows(conn, groups, "AND", where, params, limit * 3)
        hits = _score(conn, rows, groups, expr, terms, now, True) if rows else []
        if len(hits) < FEW_RESULTS:
            # Too little found: allow typos on every word and partial matches.
            groups = build_groups(conn, terms, fuzzy_for_all=True)
            rows, expr = _fts_rows(conn, groups, "OR", where, params, limit * 3)
            hits = _score(conn, rows, groups, expr, terms, now, len(terms) == 1) if rows else []
            if len(hits) < FEW_RESULTS:
                result.did_you_mean = _corrected_text(text, groups)
        result.hits = hits[:limit]

    result.suggestions = suggest(conn, text, result.did_you_mean)
    result.elapsed = time.perf_counter() - started
    return result

def nearby(conn, path: str, limit: int = 200) -> SearchResult:
    """Other files sitting in the same folder as path, newest first."""
    started = time.perf_counter()
    folder = path.rsplit("/", 1)[0] or "/"
    name = path.rsplit("/", 1)[-1]
    rows = conn.execute(
        f"SELECT {FILE_COLUMNS} FROM files f WHERE f.folder = ? AND f.path != ? "
        "ORDER BY f.mtime DESC LIMIT ?", (folder, path, limit)).fetchall()
    hits = [_hit(r) for r in rows]
    for h in hits:
        h.reason = f"same folder as {name}"
    return SearchResult(hits=hits, notes=[f"files near {name}"], elapsed=time.perf_counter() - started)

def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def suggest(conn, text: str, corrected: str = "", limit: int = 8) -> list[str]:
    """Ideas for what you might be typing: spelling fix, past searches, word endings."""
    text = text.strip()
    if not text:
        return []
    out = [corrected] if corrected else []
    low = fold(text).lower()
    rows = conn.execute(
        "SELECT query FROM history WHERE query LIKE ? ESCAPE '\\' AND query != ? "
        "ORDER BY uses DESC, ts DESC LIMIT 4", (_like_escape(low) + "%", low))
    out += [r[0] for r in rows]
    m = re.search(r"(\S+)$", low)
    if m and len(m.group(1)) >= 2:
        prefix, head = m.group(1), low[:m.start()]
        rows = conn.execute(
            "SELECT word FROM vocab WHERE word >= ? AND word < ? ORDER BY length(word) LIMIT 5",
            (prefix, prefix + "\U0010ffff"))
        out += [head + w[0] for w in rows if w[0] != prefix]
    unique = []
    for s in out:
        if s and s not in unique and s != low:
            unique.append(s)
    return unique[:limit]

def record_click(conn, query: str, path: str, now: float | None = None) -> None:
    """Learn from a click: remember that these words led to this file.

    Each (word, file) pair keeps a score that grows by one per click and fades
    with a half life of 60 days, so old habits slowly stop mattering.
    """
    now = now or time.time()
    for term in parse_query(query, now).terms:
        row = conn.execute(
            "SELECT weight, ts FROM term_clicks WHERE term = ? AND path = ?", (term, path)
        ).fetchone()
        old = 0.0
        if row:
            old = row[0] * 0.5 ** (max(0.0, now - row[1]) / 86400 / CLICK_HALF_LIFE_DAYS)
        conn.execute("INSERT OR REPLACE INTO term_clicks(term, path, weight, ts) VALUES (?, ?, ?, ?)",
                     (term, path, old + 1.0, now))
    normal = " ".join(split_words(query))
    if normal:
        conn.execute(
            "INSERT INTO history(query, uses, ts) VALUES (?, 1, ?) "
            "ON CONFLICT(query) DO UPDATE SET uses = uses + 1, ts = excluded.ts", (normal, now))
    conn.commit()

def move_click_memory(conn, old_path: str, new_path: str) -> None:
    conn.execute("UPDATE OR REPLACE term_clicks SET path = ? WHERE path = ?", (new_path, old_path))
    conn.commit()
