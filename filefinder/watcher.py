"""Live updates using Linux inotify (one watch per indexed folder).

inotify tells us when something is created, deleted, moved or finished being
written. We only turn those into small messages ("changed", path) and
("deleted", path). The indexer thread does the real work.
"""
from __future__ import annotations

import errno
import os
import threading

from inotify_simple import INotify, flags

MASK = (flags.CREATE | flags.DELETE | flags.MOVED_FROM | flags.MOVED_TO
        | flags.CLOSE_WRITE | flags.ONLYDIR | flags.DONT_FOLLOW)

class Watcher:
    def __init__(self, emit, report):
        self.emit = emit          # called with ("changed", path), ("deleted", path), ("rescan",)
        self.report = report      # called with a message for the status bar
        self.ino = INotify()
        self.paths: dict[int, str] = {}
        self.lock = threading.Lock()
        self.full = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def add(self, folder: str) -> None:
        """Watch one folder. Calling it again after a folder was moved fixes its path."""
        if self.full:
            return
        try:
            wd = self.ino.add_watch(folder, MASK)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self.full = True
                self.report("Live updates are partial: the system watch limit is used up "
                            "(raise fs.inotify.max_user_watches, see the README)")
            return
        with self.lock:
            self.paths[wd] = folder

    def close(self) -> None:
        try:
            self.ino.close()
        except OSError:
            pass

    def _loop(self) -> None:
        while True:
            try:
                events = self.ino.read(timeout=1000)
            except OSError:
                return            # the watcher was closed
            except Exception:
                continue
            for ev in events:
                self._handle(ev)

    def _handle(self, ev) -> None:
        if ev.mask & flags.Q_OVERFLOW:
            self.emit(("rescan",))
            return
        if ev.mask & flags.IGNORED:
            with self.lock:
                self.paths.pop(ev.wd, None)
            return
        with self.lock:
            base = self.paths.get(ev.wd)
        if base is None or not ev.name:
            return
        path = os.path.join(base, ev.name)
        if ev.mask & (flags.DELETE | flags.MOVED_FROM):
            self.emit(("deleted", path))
        elif ev.mask & (flags.CREATE | flags.MOVED_TO | flags.CLOSE_WRITE):
            self.emit(("changed", path))
