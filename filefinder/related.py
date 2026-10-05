"""Learn which words go together in YOUR files (no AI model needed).

Idea in everyday terms: if "tax" and "return" keep showing up in the same
file names, folder names and texts, then someone searching for "tax" probably
also wants things with "return". Counting how often two words appear together,
compared with how often you would expect it by pure chance, is enough to find
those pairs.

Two checks keep the pairs honest. First, the pair must appear together far more
often than chance (normalized pointwise mutual information). Second, the two
words must show up in mostly the same files (Jaccard overlap). The second check
stops a rare word like "berlin" from attaching itself to a broad word like
"pictures" just because every Berlin file happens to live in Pictures.
"""
from __future__ import annotations

import math
from collections import Counter
from itertools import combinations

from .textutil import split_words

# Filler and file format words say nothing about meaning.
NOISE = {
    "the", "and", "for", "with", "from", "that", "this", "new", "copy", "final", "old",
    "pdf", "jpg", "jpeg", "png", "txt", "doc", "docx", "xls", "xlsx", "zip", "img", "dsc",
    "file", "files", "untitled", "home",
}

def _doc_words(name: str, folder: str, content: str) -> list[str]:
    words = name.split() + folder.split() + split_words(content[:3000])
    seen, result = set(), []
    for w in words:
        if 3 <= len(w) <= 25 and not w.isdigit() and w not in NOISE and w not in seen:
            seen.add(w)
            result.append(w)
    return result[:60]

def learn_related(conn, max_docs: int = 30000, per_doc: int = 15, min_df: int = 3,
                  min_together: int = 3, max_df_ratio: float = 0.2, top_k: int = 8) -> int:
    """Fill the related table. Returns the number of word pairs stored.

    Looks at a sample of at most max_docs files so it stays quick and light on
    memory even with hundreds of thousands of files.
    """
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    step = max(1, total // max_docs)
    rows = conn.execute(
        "SELECT d.name, d.folder, d.content FROM files f JOIN docs d ON d.rowid = f.id "
        "WHERE f.id % ? = 0", (step,))

    documents, doc_freq = [], Counter()
    for name, folder, content in rows:
        words = _doc_words(name, folder, content)
        if len(words) >= 2:
            documents.append(words)
            doc_freq.update(words)

    n = len(documents)
    conn.execute("DELETE FROM related")
    if n < 20:
        conn.commit()
        return 0

    # Skip words that are too rare to judge or so common that they mean nothing.
    max_df = max(min_df, int(n * max_df_ratio))
    usable = {w for w, c in doc_freq.items() if min_df <= c <= max_df}

    together = Counter()
    for words in documents:
        # Keep the most specific (rarest) words of each file to bound the work.
        keep = sorted((w for w in words if w in usable), key=lambda w: (doc_freq[w], w))
        together.update(combinations(sorted(keep[:per_doc]), 2))

    best: dict[str, list] = {}
    for (a, b), count in together.items():
        if count < min_together or count >= n:
            continue
        pmi = math.log(count * n / (doc_freq[a] * doc_freq[b]))
        surprise = pmi / -math.log(count / n)       # between 0 and 1
        overlap = count / (doc_freq[a] + doc_freq[b] - count)
        if surprise >= 0.3 and overlap >= 0.25:
            best.setdefault(a, []).append((overlap, b))
            best.setdefault(b, []).append((overlap, a))

    rows_out = []
    for word, pairs in best.items():
        pairs.sort(reverse=True)
        rows_out += [(word, other, score) for score, other in pairs[:top_k]]
    conn.executemany("INSERT INTO related(word, other, score) VALUES (?, ?, ?)", rows_out)
    conn.commit()
    return len(rows_out)
