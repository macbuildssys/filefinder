"""The background worker that builds and maintains the index.

One thread does all the writing, so there are no locking surprises.
It does a scan at start (cheap when little changed), then waits for messages:
file changes from the watcher, clicks from the window, and "rescan" requests.
"""
from __future__ import annotations

import os
import queue
import re
import stat
import threading
import time
from datetime import datetime

from .config import ALWAYS_SKIP, DB_PATH, HOME, Config, is_under
from .db import connect, get_meta, path_ranges, reset_index, set_meta
from .exif import read_photo_info
from .extract import PHOTO_EXTENSIONS, kind_of, read_text, split_ext, text_extensions
from .mounts import skipped_mount_points
from .places import place_words
from .related import learn_related
from .search import move_click_memory, record_click
from .textutil import fold, split_words

COMMIT_EVERY = 2000
MAX_CONTENT_VOCAB = 300000          # cap on words that only appear inside file texts
RELATED_MIN_CHANGES = 30            # relearn related words after this many changes ...
RELATED_MIN_SECONDS = 120           # ... but at most this often
RESCAN_WITHOUT_WATCH_SECONDS = 300  # fallback when the system watch limit is used up
RESCAN_UNWATCHED_SECONDS = 900      # system folders have no live watch: refresh them this often
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")

def _words_of_path(folder: str) -> list[str]:
    """Words of a folder path, without the home folder part that every file shares."""
    rel = folder[len(HOME):] if folder == HOME or folder.startswith(HOME + "/") else folder
    return split_words(rel)

class Indexer:
    def __init__(self, cfg: Config, db_path: str = DB_PATH, status=None):
        self.cfg = cfg
        self.db_path = db_path
        self.status = status or (lambda message: None)
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.watcher = None
        self.known: set[str] = set()      # words already in the vocab table
        self.named: set[str] = set()      # those that are used in file or folder names
        self.content_vocab = 0            # how many words only come from file texts
        self.scan_id = 0
        self.visited = 0
        self.pending = 0
        self.dirty = 0                    # changes since related words were last learned
        self.last_learn = 0.0
        self.last_scan = 0.0
        self._prepare()

    def _prepare(self) -> None:
        """Work out, once per scan, which places to skip and which to read inside."""
        self.skip_mounts = skipped_mount_points() if self.cfg.whole_system else set()
        skips = list(self.cfg.exclude) + (ALWAYS_SKIP if self.cfg.whole_system else [])
        self._skip_paths = {os.path.abspath(os.path.expanduser(p)) for p in skips}
        self._skip_prefixes = tuple(p + "/" for p in self._skip_paths)
        self.deep_roots = self._clean_roots(self.cfg.deep_folders if self.cfg.whole_system
                                            else self.cfg.include)

    @staticmethod
    def _clean_roots(paths) -> list[str]:
        """Absolute, existing folders, without folders already inside another one in the list."""
        roots = sorted({os.path.abspath(os.path.expanduser(p)) for p in paths})
        roots = [r for r in roots if os.path.isdir(r)]
        return [r for r in roots if not any(r != o and is_under(r, o) for o in roots)]

    def is_deep(self, path: str) -> bool:
        """Deep places are read inside (text, photo details) and watched live."""
        return any(is_under(path, root) for root in self.deep_roots)

    # Starting and stopping

    def start(self, live: bool = True) -> None:
        if live:
            try:
                from .watcher import Watcher
                self.watcher = Watcher(self.events.put, self.status)
                self.watcher.start()
            except Exception as exc:   # inotify missing or blocked
                self.watcher = None
                self.status(f"Live updates unavailable: {exc}")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.watcher:
            self.watcher.close()
        if self.thread:
            self.thread.join(timeout=5)

    def submit(self, event: tuple) -> None:
        self.events.put(event)

    # Main loop

    def _run(self) -> None:
        try:
            os.nice(10)    # on Linux this lowers only this thread, so the window stays quick
        except OSError:
            pass
        conn = connect(self.db_path)
        try:
            for word, src in conn.execute("SELECT word, src FROM vocab"):
                self.known.add(word)
                if src:
                    self.named.add(word)
            self.content_vocab = len(self.known) - len(self.named)
            self._safely(self.rescan, conn)
            while not self.stop_event.is_set():
                try:
                    batch = [self.events.get(timeout=1.0)]
                except queue.Empty:
                    self._idle_work(conn)
                    continue
                deadline = time.time() + 0.5      # gather a burst of events into one batch
                while time.time() < deadline:
                    try:
                        batch.append(self.events.get(timeout=0.1))
                    except queue.Empty:
                        pass
                self._handle_batch(conn, batch)
        finally:
            conn.close()

    def _safely(self, function, *args) -> None:
        try:
            function(*args)
        except Exception as exc:
            self.status(f"Indexer problem: {exc}")

    def _handle_batch(self, conn, batch) -> None:
        seen = set()
        for event in batch:
            if event in seen:
                continue
            seen.add(event)
            self._safely(self._dispatch, conn, event)
        conn.commit()
        self.status(self._summary(conn))
        self._idle_work(conn)

    def _idle_work(self, conn) -> None:
        """Chores that do not need to happen on every single change."""
        now = time.time()
        if self.dirty >= RELATED_MIN_CHANGES and now - self.last_learn >= RELATED_MIN_SECONDS:
            self._safely(self._learn, conn)
        watch_limit_hit = self.watcher is not None and getattr(self.watcher, "full", False)
        if watch_limit_hit and now - self.last_scan >= RESCAN_WITHOUT_WATCH_SECONDS:
            self._safely(self.rescan, conn)   # folders without a watch are caught up by rescanning
        elif self.cfg.whole_system and now - self.last_scan >= RESCAN_UNWATCHED_SECONDS:
            self._safely(self.rescan, conn)   # system folders are not watched, so refresh them

    def _dispatch(self, conn, event: tuple) -> None:
        kind = event[0]
        if kind == "changed":
            self._on_changed(conn, event[1])
            self.dirty += 1
        elif kind == "deleted":
            self._delete_tree(conn, event[1])
            self.dirty += 1
        elif kind == "click":
            record_click(conn, event[1], event[2])
        elif kind == "renamed":
            move_click_memory(conn, event[1], event[2])
        elif kind == "rescan":
            self.rescan(conn)
        elif kind == "rebuild":
            reset_index(conn)
            self.known, self.named, self.content_vocab = set(), set(), 0
            self.rescan(conn)

    def _summary(self, conn) -> str:
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return f"Up to date: {count:,} items indexed"

    # Scanning

    def _roots(self) -> list[str]:
        if self.cfg.whole_system:
            return ["/"]
        return self._clean_roots(self.cfg.include)

    def _skipped(self, path: str, name: str, is_dir: bool) -> bool:
        if self.cfg.skip_hidden and name.startswith("."):
            return True
        if is_dir and (name in self.cfg.exclude_names or path in self.skip_mounts):
            return True
        return path in self._skip_paths or path.startswith(self._skip_prefixes)

    def rescan(self, conn) -> None:
        """Walk all chosen folders, add what is new, drop what disappeared."""
        self.last_scan = time.time()
        self._prepare()
        self.scan_id = int(get_meta(conn, "scan_id", "0")) + 1
        set_meta(conn, "scan_id", str(self.scan_id))
        self.visited = 0
        changed = 0
        for root in self._roots():
            changed += self._walk(conn, root)
            if self.stop_event.is_set():
                return
        conn.commit()
        conn.execute("DELETE FROM docs WHERE rowid IN (SELECT id FROM files WHERE seen < ?)",
                     (self.scan_id,))
        conn.execute("DELETE FROM files WHERE seen < ?", (self.scan_id,))
        conn.commit()
        self._extract_pass(conn)
        if changed >= 200 or not get_meta(conn, "related_built"):
            self._learn(conn)
        self.status(self._summary(conn))

    def _walk(self, conn, top: str) -> int:
        changed = 0
        stack = [top]
        while stack and not self.stop_event.is_set():
            folder = stack.pop()
            if self.watcher and self.is_deep(folder):
                self.watcher.add(folder)
            try:
                entries = os.scandir(folder)
            except OSError:
                continue
            with entries:
                try:
                    for entry in entries:
                        changed += self._visit(conn, entry, stack)
                except OSError:
                    pass
        return changed

    def _visit(self, conn, entry, stack) -> int:
        try:
            if entry.is_symlink():            # never follow links
                return 0
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not entry.is_file(follow_symlinks=False):
                return 0                       # sockets, pipes, devices
            path, name = entry.path, entry.name
            path.encode("utf-8")               # skip names SQLite cannot store
            if self._skipped(path, name, is_dir):
                return 0
            info = entry.stat(follow_symlinks=False)
        except (OSError, UnicodeError):
            return 0
        if is_dir:
            stack.append(path)
        self.visited += 1
        if self.visited % 5000 == 0:
            self.status(f"Scanning folders: {self.visited:,} items")
        size = 0 if is_dir else info.st_size
        return int(self._upsert(conn, path, name, is_dir, size, info.st_mtime))

    def _upsert(self, conn, path, name, is_dir, size, mtime) -> bool:
        """Insert or refresh one row. Returns True when something changed."""
        row = conn.execute("SELECT id, size, mtime FROM files WHERE path = ?", (path,)).fetchone()
        if row and row[1] == size and abs(row[2] - mtime) < 1e-6:
            conn.execute("UPDATE files SET seen = ? WHERE id = ?", (self.scan_id, row[0]))
            return False
        folder = os.path.dirname(path)
        ext = "" if is_dir else split_ext(name)
        kind = kind_of(ext, is_dir)
        name_words, folder_words = split_words(name), _words_of_path(folder)
        self._remember_words(conn, name_words + folder_words, named=True)
        if row:
            conn.execute(
                "UPDATE files SET name=?, folder=?, ext=?, kind=?, size=?, mtime=?, seen=? WHERE id=?",
                (name, folder, ext, kind, size, mtime, self.scan_id, row[0]))
            conn.execute("UPDATE docs SET name = ?, folder = ? WHERE rowid = ?",
                         (" ".join(name_words), " ".join(folder_words), row[0]))
        else:
            cur = conn.execute(
                "INSERT INTO files(path, name, folder, ext, kind, is_dir, size, mtime, seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, name, folder, ext, kind, int(is_dir), size, mtime, self.scan_id))
            conn.execute("INSERT INTO docs(rowid, name, folder, content, extra) VALUES (?, ?, ?, '', '')",
                         (cur.lastrowid, " ".join(name_words), " ".join(folder_words)))
        self.pending += 1
        if self.pending >= COMMIT_EVERY:
            conn.commit()
            self.pending = 0
        return True

    def _remember_words(self, conn, words, named: bool) -> None:
        """Keep the list of words used for typo matching up to date."""
        for w in words:
            if w.isdigit() or not 2 <= len(w) <= 30:
                continue
            if w in self.known:
                if named and w not in self.named:
                    conn.execute("UPDATE vocab SET src = 1 WHERE word = ?", (w,))
                    self.named.add(w)
                continue
            self.known.add(w)
            if named:
                self.named.add(w)
            else:
                self.content_vocab += 1
            cur = conn.execute("INSERT OR IGNORE INTO vocab(word, src) VALUES (?, ?)", (w, int(named)))
            if cur.rowcount:
                conn.execute("INSERT INTO vocab_tri(rowid, word) VALUES (?, ?)", (cur.lastrowid, w))

    def _remember_text_words(self, conn, text: str) -> None:
        """Let words from inside files take part in typo matching too (up to a cap)."""
        if self.content_vocab >= MAX_CONTENT_VOCAB or not text:
            return
        found = {fold(w) for w in re.findall(r"[^\W\d_]{4,20}", text.lower())}
        fresh = [w for w in found if w not in self.known][:200]
        self._remember_words(conn, fresh, named=False)

    # Text and photo details

    def _wants(self, ext: str) -> bool:
        if ext in PHOTO_EXTENSIONS:
            return self.cfg.index_photos
        return self.cfg.index_text and ext in text_extensions(self.cfg.use_pdftotext)

    def _extract_pass(self, conn) -> None:
        exts = sorted(e for e in (text_extensions(self.cfg.use_pdftotext) | PHOTO_EXTENSIONS)
                      if self._wants(e))
        if not exts:
            return
        marks = ",".join("?" * len(exts))
        where, params = path_ranges("path", self.deep_roots)
        place = f" AND {where}" if where else ""
        rows = conn.execute(
            "SELECT id, path, ext, size, mtime FROM files "
            f"WHERE is_dir = 0 AND content_mtime < mtime AND ext IN ({marks}){place}",
            exts + params).fetchall()
        for i, (id_, path, ext, size, mtime) in enumerate(rows, 1):
            if self.stop_event.is_set():
                break
            self._store_details(conn, id_, path, ext, size, mtime)
            if i % 300 == 0:
                conn.commit()
                self.status(f"Reading file contents and photo details: {i:,} of {len(rows):,}")
        conn.commit()

    def _store_details(self, conn, id_, path, ext, size, mtime) -> None:
        if ext in PHOTO_EXTENSIONS:
            self._store_photo(conn, id_, path, mtime)
            return
        text = read_text(path, ext, size, self.cfg.use_pdftotext)
        conn.execute("UPDATE docs SET content = ? WHERE rowid = ?", (text, id_))
        conn.execute("UPDATE files SET content_mtime = ? WHERE id = ?", (mtime, id_))
        self._remember_text_words(conn, text)

    def _store_photo(self, conn, id_, path, mtime) -> None:
        """Date taken goes into its own column. Place and date words go into the search text."""
        info = read_photo_info(path)
        taken, words = None, []
        if info:
            taken = info.taken
            if info.lat is not None and info.lon is not None:
                try:
                    words += place_words(info.lat, info.lon)
                except (OSError, ValueError):
                    pass                        # place data missing: dates still work
            if taken:
                when = datetime.fromtimestamp(taken)
                words += [str(when.year), MONTHS[when.month - 1]]
        self._remember_words(conn, words, named=True)
        conn.execute("UPDATE docs SET extra = ? WHERE rowid = ?", (" ".join(words), id_))
        conn.execute("UPDATE files SET taken = ?, content_mtime = ? WHERE id = ?", (taken, mtime, id_))

    def _learn(self, conn) -> None:
        self.status("Learning which words go together in your files")
        learn_related(conn)
        self.dirty = 0
        self.last_learn = time.time()
        set_meta(conn, "related_built", str(self.last_learn))
        conn.commit()

    # Reacting to changes

    def _in_roots(self, path: str) -> bool:
        return any(is_under(path, r) for r in self._roots())

    def _on_changed(self, conn, path: str) -> None:
        name = os.path.basename(path)
        if not self._in_roots(path) or self._skipped(path, name, False):
            return
        try:
            info = os.lstat(path)
        except OSError:
            return
        if stat.S_ISDIR(info.st_mode):
            if self._skipped(path, name, True):
                return
            self._upsert(conn, path, name, True, 0, info.st_mtime)
            self._walk(conn, path)             # new folder: index inside it, add watches
            self._extract_pass(conn)
        elif stat.S_ISREG(info.st_mode):
            self._upsert(conn, path, name, False, info.st_size, info.st_mtime)
            row = conn.execute("SELECT id, ext FROM files WHERE path = ?", (path,)).fetchone()
            if row and self._wants(row[1]) and self.is_deep(path):
                self._store_details(conn, row[0], path, row[1], info.st_size, info.st_mtime)

    def _delete_tree(self, conn, path: str) -> None:
        """Remove a file, or a folder with everything inside it."""
        # Paths inside a folder sort between "folder/" and "folder0", so a range
        # lookup finds the whole subtree without any wildcard matching.
        where = "path = ? OR (path > ? AND path < ?)"
        params = (path, path + "/", path + "0")
        conn.execute(f"DELETE FROM docs WHERE rowid IN (SELECT id FROM files WHERE {where})", params)
        conn.execute(f"DELETE FROM files WHERE {where}", params)
