"""The SQLite index: one file on disk, no server.

Tables
  files       one row per file or folder (name, path, size, date, date taken, type)
  docs        full text index (SQLite FTS5) of name, folder words, file text and
              extra details (place and date words from photos)
  vocab       every word seen in file names, folder names, photo places and file text
              (src is 1 for words used in names, 0 for words only seen in text)
  vocab_tri   3 letter pieces of those words, used to find typos fast
  related     word pairs that often appear together in your files
  term_clicks what you opened after searching for each word
  history     your past searches
"""
from __future__ import annotations

import os
import sqlite3

from .config import DB_PATH

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    content_mtime REAL NOT NULL DEFAULT 0,
    taken REAL
);
CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS files_kind_mtime ON files(kind, mtime);
CREATE INDEX IF NOT EXISTS files_name ON files(name COLLATE NOCASE);

CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    name, folder, content, extra,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3'
);

CREATE TABLE IF NOT EXISTS vocab(
    id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE,
    src INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS vocab_tri USING fts5(word, tokenize = 'trigram');

CREATE TABLE IF NOT EXISTS related(
    word TEXT NOT NULL, other TEXT NOT NULL, score REAL NOT NULL,
    PRIMARY KEY(word, other)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS term_clicks(
    term TEXT NOT NULL, path TEXT NOT NULL, weight REAL NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY(term, path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS history(
    query TEXT PRIMARY KEY, uses INTEGER NOT NULL, ts REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""

def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """Open the index. Every thread must open its own connection."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    is_new = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=15)
    if is_new:
        os.chmod(path, 0o600)  # the index holds file names and text snippets
    conn.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    """Create the tables. An index from an older version is dropped and rebuilt by the
    next scan, while what was learned from your clicks and searches is kept."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        for table in ("files", "docs", "vocab_tri", "vocab", "related"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    if version < SCHEMA_VERSION:
        conn.execute("DELETE FROM meta WHERE key = 'related_built'")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()

def reset_index(conn: sqlite3.Connection) -> None:
    """Forget all files but keep what was learned from your clicks."""
    for table in ("files", "docs", "vocab", "vocab_tri", "related"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM meta WHERE key = 'related_built'")
    conn.commit()

def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

def path_ranges(column: str, roots: list[str]):
    """SQL saying 'column is inside one of these folders', as range lookups (no wildcards).

    Paths inside a folder sort between "folder/" and "folder0". Returns ("", []) when the
    roots cover everything, so callers can skip the condition.
    """
    parts, params = [], []
    for root in roots:
        root = root.rstrip("/") or "/"
        if root == "/":
            return "", []
        parts.append(f"({column} > ? AND {column} < ?)")
        params += [root + "/", root + "0"]
    if not parts:
        return "0", []          # no folders at all: nothing qualifies
    return "(" + " OR ".join(parts) + ")", params
