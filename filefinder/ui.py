"""The window. All file names and texts are shown as plain text, never as markup."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime

from PySide6.QtCore import (QAbstractTableModel, QFile, QMimeDatabase, QModelIndex, QObject,
                            QRunnable, QStringListModel, Qt, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog,
                               QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton, QTableView,
                               QVBoxLayout, QWidget)

from .config import Config, save_config
from .db import connect
from .fileops import move_path
from .indexer import Indexer
from .openwith import apps_for_mime, clean_env, launch, load_apps, open_default, system_default_id
from .search import SearchResult, nearby, run_search

COLUMNS = ["Name", "Path", "Folder", "Type", "Size", "Modified", "Taken", "Why it matched"]
COLUMN_WIDTHS = [240, 380, 260, 110, 80, 130, 130, 380]

# Opening these may start a program, so we ask first.
RUNNABLE_EXTENSIONS = {"desktop", "sh", "run", "appimage", "bin", "exe", "bat", "jar", "py",
                       "pl", "rb", "command", "flatpakref", "deb", "rpm"}

SHORTCUTS_HELP = """Ctrl+L or Ctrl+K     jump to the search box
Down or Enter         move from the search box to the results
Enter                 open the selected file (in the results)
Ctrl+O                open the selected file from anywhere
Ctrl+Shift+O          open with another application
Ctrl+Return           open the folder that contains it
Ctrl+Shift+C          copy the full path
F2                    rename
Shift+F2              move to another folder
Delete                move to the trash
Ctrl+N                show other files in the same folder
Esc                   clear the search
Ctrl+,                settings
Ctrl+R                rescan now
Ctrl+Q                quit"""


def format_size(hit) -> str:
    if hit.kind == "folder":
        return ""
    size = float(hit.size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


# Searching happens off the window thread so typing never stutters

_local = threading.local()


def _thread_connection(db_path: str):
    if getattr(_local, "conn", None) is None:
        _local.conn = connect(db_path)
    return _local.conn


class SearchSignals(QObject):
    done = Signal(int, object)


class SearchTask(QRunnable):
    def __init__(self, seq, mode, text, db_path, signals, scope=None):
        super().__init__()
        self.seq, self.mode, self.text = seq, mode, text
        self.db_path, self.signals, self.scope = db_path, signals, scope

    def run(self):
        try:
            conn = _thread_connection(self.db_path)
            if self.mode == "nearby":
                result = nearby(conn, self.text)
            else:
                result = run_search(conn, self.text, scope=self.scope)
        except Exception as exc:
            result = SearchResult(error=str(exc))
        self.signals.done.emit(self.seq, result)


class MoveSignals(QObject):
    finished = Signal(str, str, str)      # old path, new path, error text (empty when it worked)


class MoveTask(QRunnable):
    """Moves a file or folder, also to another drive, without freezing the window."""

    def __init__(self, src, dst, signals):
        super().__init__()
        self.src, self.dst, self.signals = src, dst, signals

    def run(self):
        error = ""
        try:
            move_path(self.src, self.dst)
        except OSError as exc:
            error = exc.strerror or str(exc)
        except Exception as exc:
            error = str(exc)
        self.signals.finished.emit(self.src, self.dst, error)


# The results table

class ResultsModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.hits: list = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.hits)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        h, col = self.hits[index.row()], index.column()
        if role == Qt.DisplayRole:
            return (h.name, h.path, h.folder, h.label, format_size(h), format_time(h.mtime),
                    format_time(h.taken) if h.taken else "", h.reason)[col]
        if role == Qt.ToolTipRole:
            return h.reason if col == 7 else h.path
        if role == Qt.TextAlignmentRole and col == 4:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def set_hits(self, hits):
        self.beginResetModel()
        self.hits = list(hits)
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        if column < 0:
            return
        keys = (lambda h: h.name.lower(), lambda h: h.path.lower(), lambda h: h.folder.lower(),
                lambda h: h.label, lambda h: h.size, lambda h: h.mtime,
                lambda h: h.taken or 0, lambda h: h.reason)
        self.layoutAboutToBeChanged.emit()
        self.hits.sort(key=keys[column], reverse=(order == Qt.DescendingOrder))
        self.layoutChanged.emit()

    def update_hit(self, row, **changes):
        for key, value in changes.items():
            setattr(self.hits[row], key, value)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def find(self, path):
        for row, h in enumerate(self.hits):
            if h.path == path:
                return row
        return -1

    def remove_row(self, row):
        self.beginRemoveRows(QModelIndex(), row, row)
        del self.hits[row]
        self.endRemoveRows()


class SearchBox(QLineEdit):
    down_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Down:
            self.down_pressed.emit()
        elif event.key() == Qt.Key_Escape and self.text():
            self.clear()
        else:
            super().keyPressEvent(event)


# Open with

class OpenWithDialog(QDialog):
    """Pick the application to open a file with."""

    def __init__(self, file_name, mime, apps, matching, default_id, remembered_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open with")
        self.resize(460, 520)
        self.apps, self.matching = apps, matching
        self.default_id, self.remembered_id = default_id, remembered_id
        self.chosen_app = None
        self.remember = False
        layout = QVBoxLayout(self)
        title = QLabel(f"Open {file_name} with:")
        title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        default_name = apps[default_id].name if default_id in apps else "none found"
        info = QLabel(f"File type: {mime}\nThe system would normally use: {default_name}")
        info.setTextFormat(Qt.PlainText)
        info.setStyleSheet("color: palette(mid);")
        layout.addWidget(info)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter applications")
        self.filter.textChanged.connect(self._fill)
        layout.addWidget(self.filter)
        self.listing = QListWidget()
        self.listing.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self.listing, 1)
        self.show_all = QCheckBox("Show all installed applications, not just the ones for this file type")
        self.show_all.toggled.connect(self._fill)
        layout.addWidget(self.show_all)
        self.always = QCheckBox("Always use this choice for this file type (inside File Finder only)")
        layout.addWidget(self.always)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fill()

    def _fill(self, *_):
        self.listing.clear()
        self.listing.addItem("Use the system default")
        pool = sorted(self.apps.values(), key=lambda a: a.name.lower()) \
            if self.show_all.isChecked() else self.matching
        needle = self.filter.text().strip().lower()
        select = self.listing.item(0)
        for app in pool:
            if needle and needle not in app.name.lower():
                continue
            tag = (" (your choice)" if app.id == self.remembered_id
                   else " (system default)" if app.id == self.default_id else "")
            item = QListWidgetItem(app.name + tag)
            item.setData(Qt.UserRole, app.id)
            if app.icon:
                item.setIcon(QIcon.fromTheme(app.icon))
            self.listing.addItem(item)
            if app.id == self.remembered_id:
                select = item
        self.listing.setCurrentItem(select)

    def accept(self):
        item = self.listing.currentItem()
        if item is None:
            return
        self.chosen_app = self.apps.get(item.data(Qt.UserRole))   # None means the system default
        self.remember = self.always.isChecked()
        super().accept()


# Settings

class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, first_run: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Finder: welcome" if first_run else "Settings")
        self.resize(620, 760)
        self.open_with = dict(cfg.open_with)
        layout = QVBoxLayout(self)
        if first_run:
            hello = QLabel("Everything stays on this computer: the index is a file in your home "
                           "folder and nothing is sent anywhere.")
            hello.setWordWrap(True)
            layout.addWidget(hello)
        self.whole = QCheckBox("Search the whole computer (every file and folder you are allowed to see)")
        self.whole.setChecked(cfg.whole_system)
        layout.addWidget(self.whole)
        note = QLabel("Every file is found by name. Your home folder and external drives are also "
                      "read inside and kept up to date live. System folders are refreshed every "
                      "15 minutes. Places like /proc and network drives are always skipped.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        self.deep_group, self.deep = self._folder_box(
            layout, "Read inside files and update live", cfg.deep_folders)
        self.include_group, self.include = self._folder_box(
            layout, "Folders to search (used when the whole computer is switched off)", cfg.include)
        self.exclude_group, self.exclude = self._folder_box(
            layout, "Folders to skip (with everything inside)", cfg.exclude)
        self.whole.toggled.connect(self._update_enabled)
        self._update_enabled()
        self.hidden = QCheckBox("Skip hidden files and folders (names starting with a dot)")
        self.hidden.setChecked(cfg.skip_hidden)
        self.text = QCheckBox("Read the words inside text, Word, LibreOffice and e-book files "
                              "(first 20 KB of each)")
        self.text.setChecked(cfg.index_text)
        self.photos = QCheckBox("Read date taken and place from photos (place names come from a "
                                "built in list, nothing is looked up online)")
        self.photos.setChecked(cfg.index_photos)
        self.pdf = QCheckBox("Also read the first pages of PDFs (needs pdftotext from poppler-utils)")
        self.pdf.setChecked(cfg.use_pdftotext)
        self.ask = QCheckBox("Always ask which application to open files with")
        self.ask.setChecked(cfg.always_ask_open)
        for box in (self.hidden, self.text, self.photos, self.pdf, self.ask):
            layout.addWidget(box)
        self.forget_button = QPushButton("Forget remembered applications")
        self.forget_button.clicked.connect(self._forget)
        self.forget_button.setEnabled(bool(cfg.open_with))
        layout.addWidget(self.forget_button, 0, Qt.AlignLeft)
        self.names = cfg.exclude_names
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_enabled(self, *_):
        self.deep_group.setEnabled(self.whole.isChecked())
        self.include_group.setEnabled(not self.whole.isChecked())

    def _forget(self):
        self.open_with.clear()
        self.forget_button.setText("Remembered applications will be forgotten when you press OK")
        self.forget_button.setEnabled(False)

    def _folder_box(self, layout, title, items):
        group = QGroupBox(title)
        inner = QVBoxLayout(group)
        listing = QListWidget()
        listing.addItems(items)
        row = QHBoxLayout()
        add, remove = QPushButton("Add folder"), QPushButton("Remove selected")
        add.clicked.connect(lambda: self._add(listing))
        remove.clicked.connect(lambda: [listing.takeItem(listing.row(i)) for i in listing.selectedItems()])
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        inner.addWidget(listing)
        inner.addLayout(row)
        layout.addWidget(group)
        return group, listing

    def _add(self, listing):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", os.path.expanduser("~"))
        if folder and folder not in [listing.item(i).text() for i in range(listing.count())]:
            listing.addItem(folder)

    def result_config(self) -> Config:
        def items(w):
            return [w.item(i).text() for i in range(w.count())]
        return Config(whole_system=self.whole.isChecked(), include=items(self.include),
                      deep_folders=items(self.deep), exclude=items(self.exclude),
                      exclude_names=self.names, skip_hidden=self.hidden.isChecked(),
                      index_text=self.text.isChecked(), index_photos=self.photos.isChecked(),
                      use_pdftotext=self.pdf.isChecked(), always_ask_open=self.ask.isChecked(),
                      open_with=dict(self.open_with))


# Main window

class MainWindow(QMainWindow):
    index_status = Signal(str)

    def __init__(self, cfg: Config, db_path: str):
        super().__init__()
        self.cfg, self.db_path = cfg, db_path
        self.setWindowTitle("File Finder")
        self.resize(1250, 720)
        self.seq = 0
        self.signals = SearchSignals()
        self.signals.done.connect(self._on_results)
        self.move_signals = MoveSignals()
        self.move_signals.finished.connect(self._on_moved)
        self.move_pool = QThreadPool()
        self.move_pool.setMaxThreadCount(1)      # one move at a time
        self.indexer = Indexer(cfg, db_path, status=self.index_status.emit)
        self.index_status.connect(self._on_index_status)
        self._build_ui()
        self._build_menu()
        self._build_shortcuts()

    def start_indexing(self):
        self.indexer.start(live=True)

    def closeEvent(self, event):
        self.indexer.stop()
        super().closeEvent(event)

    # Building the window

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        self.box = SearchBox()
        self.box.setPlaceholderText('Search files and folders, for example "invoice", '
                                    '"tax 2025", "PDFs in Downloads", "large videos last month"')
        self.box.setClearButtonEnabled(True)
        self.box.textChanged.connect(lambda: self.timer.start(120))
        self.box.returnPressed.connect(self._focus_results)
        self.box.down_pressed.connect(self._focus_results)
        top = QHBoxLayout()
        top.addWidget(self.box, 1)
        self.scope_box = QComboBox()
        self.scope_box.addItems(["System-wide", "My folders only"])
        self.scope_box.setToolTip("System-wide includes system folders. My folders only limits "
                                  "results to your home folder and external drives.")
        self.scope_box.currentIndexChanged.connect(lambda _: self._start_search())
        self.scope_box.setVisible(self.cfg.whole_system)
        top.addWidget(self.scope_box)
        layout.addLayout(top)

        self.completer_model = QStringListModel()
        self.completer = QCompleter(self.completer_model, self)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.box.setCompleter(self.completer)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._start_search)

        info = QHBoxLayout()
        self.notes = QLabel("")
        self.notes.setTextFormat(Qt.PlainText)
        self.notes.setStyleSheet("color: palette(mid);")
        self.dym = QPushButton("")
        self.dym.setFlat(True)
        self.dym.setVisible(False)
        self.dym.clicked.connect(lambda: self.box.setText(self.dym.property("fix")))
        info.addWidget(self.notes, 1)
        info.addWidget(self.dym)
        layout.addLayout(info)

        self.model = ResultsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, width in enumerate(COLUMN_WIDTHS):
            self.table.setColumnWidth(i, width)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        self.table.selectionModel().currentRowChanged.connect(self._show_selected_path)
        layout.addWidget(self.table, 1)

        self.path_label = QLabel("")
        self.path_label.setTextFormat(Qt.PlainText)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.setCentralWidget(central)

        self.index_label = QLabel("Starting")
        self.statusBar().addPermanentWidget(self.index_label)
        self.box.setFocus()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")
        for text, shortcut, handler in (
                ("Settings...", "Ctrl+,", self.open_settings),
                ("Rescan now", "Ctrl+R", lambda: self.indexer.submit(("rescan",))),
                ("Rebuild index from scratch", "", self.rebuild_index),
                ("Quit", "Ctrl+Q", self.close)):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            file_menu.addAction(action)
        help_menu = self.menuBar().addMenu("&Help")
        keys = QAction("Keyboard shortcuts", self)
        keys.triggered.connect(lambda: QMessageBox.information(self, "Keyboard shortcuts", SHORTCUTS_HELP))
        help_menu.addAction(keys)

    def _build_shortcuts(self):
        def window(keys, handler):
            QShortcut(QKeySequence(keys), self).activated.connect(handler)

        def in_table(keys, handler):
            shortcut = QShortcut(QKeySequence(keys), self.table)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(handler)

        window("Ctrl+L", self._focus_search)
        window("Ctrl+K", self._focus_search)
        window("Ctrl+O", self.open_selected)
        window("Ctrl+Shift+O", self.open_with)
        window("Ctrl+Return", self.open_folder)
        window("Ctrl+Enter", self.open_folder)
        window("Ctrl+Shift+C", self.copy_path)
        window("Ctrl+N", self.show_nearby)
        window("Shift+F2", self.move_selected)
        in_table("Return", self.open_selected)
        in_table("Enter", self.open_selected)
        in_table("F2", self.rename_selected)
        in_table("Delete", self.trash_selected)

    # Searching

    def _start_search(self, mode="text", argument=""):
        text = argument if mode == "nearby" else self.box.text().strip()
        self.seq += 1
        if not text:
            self.model.set_hits([])
            self.notes.setText("")
            self.dym.setVisible(False)
            self.statusBar().clearMessage()
            return
        scope = self._scope_roots() if mode == "text" else None
        QThreadPool.globalInstance().start(
            SearchTask(self.seq, mode, text, self.db_path, self.signals, scope))

    def _scope_roots(self):
        """Folders to limit the search to, or None to search system-wide."""
        if self.cfg.whole_system and self.scope_box.currentIndex() == 1:
            return [os.path.abspath(os.path.expanduser(p)) for p in self.cfg.deep_folders]
        return None

    def _on_results(self, seq: int, result: SearchResult):
        if seq != self.seq:
            return                        # a newer search already replaced this one
        if result.error:
            self.statusBar().showMessage(f"Search problem: {result.error}", 6000)
            return
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.model.set_hits(result.hits)
        if result.hits:
            self.table.selectRow(0)
        parts = (["words: " + ", ".join(result.terms)] if result.terms else []) + result.notes
        self.notes.setText("Looking for: " + "; ".join(parts) if parts else "")
        if result.did_you_mean:
            self.dym.setText(f"Did you mean: {result.did_you_mean}?")
            self.dym.setProperty("fix", result.did_you_mean)
            self.dym.setVisible(True)
        else:
            self.dym.setVisible(False)
        self.completer_model.setStringList(result.suggestions)
        if result.suggestions and self.box.hasFocus():
            self.completer.complete()
        shown = f"{len(result.hits):,} results in {result.elapsed * 1000:.0f} ms"
        self.statusBar().showMessage(shown if result.hits else "No matches. " + shown)

    def _on_index_status(self, message: str):
        self.index_label.setText(message)

    # Helpers for the selected result

    def current_hit(self):
        index = self.table.currentIndex()
        return self.model.hits[index.row()] if index.isValid() else None

    def _show_selected_path(self, *_):
        hit = self.current_hit()
        self.path_label.setText(hit.path if hit else "")

    def _focus_search(self):
        self.box.setFocus()
        self.box.selectAll()

    def _focus_results(self):
        if self.model.rowCount():
            self.table.setFocus()
            if not self.table.currentIndex().isValid():
                self.table.selectRow(0)

    def _learn(self, hit):
        """Tell the indexer that this search led to this file."""
        self.indexer.submit(("click", self.box.text(), hit.path))

    def _warn(self, text: str):
        QMessageBox.warning(self, "File Finder", text)

    def _exists(self, hit) -> bool:
        if os.path.lexists(hit.path):
            return True
        self.statusBar().showMessage("That file no longer exists. The index will catch up shortly.", 5000)
        return False

    # Actions

    def open_selected(self):
        hit = self.current_hit()
        if not hit or not self._exists(hit):
            return
        ext = hit.path.rsplit(".", 1)[-1].lower() if "." in hit.name else ""
        runnable = not os.path.isdir(hit.path) and (ext in RUNNABLE_EXTENSIONS
                                                    or os.access(hit.path, os.X_OK))
        if runnable and not self.cfg.always_ask_open:
            answer = QMessageBox.question(
                self, "Open this file?",
                f"{hit.name} may start a program when opened.\n\nOpen it anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self._learn(hit)
        if self.cfg.always_ask_open:
            return self._open_with_dialog(hit)
        remembered = self.cfg.open_with.get(self._mime_names(hit.path)[0])
        app = load_apps().get(remembered) if remembered else None
        if app:
            return self._start_app(app, hit)       # your own choice for this file type
        if not self._open_default(hit.path):
            self._open_with_dialog(hit)            # nothing is set for this type: let the person pick

    def open_with(self):
        hit = self.current_hit()
        if hit and self._exists(hit):
            self._learn(hit)
            self._open_with_dialog(hit)

    def _mime_names(self, path: str) -> list:
        """The file type and its more general parents, for example text/markdown, text/plain."""
        mime = QMimeDatabase().mimeTypeForFile(path)
        return [mime.name()] + list(mime.allAncestors())

    def _open_with_dialog(self, hit):
        mimes = self._mime_names(hit.path)
        apps = load_apps()
        dialog = OpenWithDialog(hit.name, mimes[0], apps, apps_for_mime(apps, mimes),
                                system_default_id(mimes[0]), self.cfg.open_with.get(mimes[0]), self)
        if dialog.exec() != QDialog.Accepted:
            return
        if dialog.remember:
            if dialog.chosen_app:
                self.cfg.open_with[mimes[0]] = dialog.chosen_app.id
            else:
                self.cfg.open_with.pop(mimes[0], None)    # back to the system default
            save_config(self.cfg)
        if dialog.chosen_app:
            self._start_app(dialog.chosen_app, hit)
        else:
            self._open_default(hit.path)

    def _open_default(self, target: str) -> bool:
        if open_default(target):
            return True
        self.statusBar().showMessage("Nothing is set to open this file type. "
                                     "Choose an application with Open with.", 8000)
        return False

    def _start_app(self, app, hit):
        try:
            launch(app, hit.path)
        except (OSError, ValueError) as exc:
            self._warn(f"Could not start {app.name}: {exc}")

    def open_folder(self):
        hit = self.current_hit()
        if not hit or not self._exists(hit):
            return
        self._learn(hit)
        dolphin = shutil.which("dolphin")
        if dolphin:   # opens the folder with the file highlighted
            subprocess.Popen([dolphin, "--select", hit.path], start_new_session=True, env=clean_env())
        else:
            self._open_default(hit.folder)

    def copy_path(self):
        hit = self.current_hit()
        if hit:
            QGuiApplication.clipboard().setText(hit.path)
            self._learn(hit)
            self.statusBar().showMessage("Path copied", 3000)

    def show_nearby(self):
        hit = self.current_hit()
        if hit:
            self._start_search("nearby", hit.path)

    def rename_selected(self):
        hit = self.current_hit()
        if not hit or not self._exists(hit):
            return
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=hit.name)
        if not ok or new == hit.name:
            return
        if not new or "/" in new or "\x00" in new or new in (".", ".."):
            return self._warn("That is not a valid file name.")
        target = os.path.join(hit.folder, new)
        if os.path.lexists(target):
            return self._warn("Something with that name already exists in this folder.")
        try:
            os.rename(hit.path, target)
        except OSError as exc:
            return self._warn(f"Could not rename: {exc.strerror}")
        self.indexer.submit(("renamed", hit.path, target))
        self.model.update_hit(self.table.currentIndex().row(), path=target, name=new)
        self._show_selected_path()

    def move_selected(self):
        hit = self.current_hit()
        if not hit or not self._exists(hit):
            return
        folder = QFileDialog.getExistingDirectory(self, "Move to which folder?", hit.folder)
        if not folder:
            return
        target = os.path.join(folder, hit.name)
        if os.path.lexists(target):
            return self._warn("Something with that name already exists there.")
        self.statusBar().showMessage(f"Moving {hit.name} (you can keep searching)...")
        self.move_pool.start(MoveTask(hit.path, target, self.move_signals))

    def _on_moved(self, old: str, new: str, error: str):
        if error:
            self.statusBar().clearMessage()
            return self._warn(f"Could not move it: {error}")
        self.statusBar().showMessage("Moved", 4000)
        self.indexer.submit(("renamed", old, new))
        row = self.model.find(old)
        if row >= 0:
            self.model.update_hit(row, path=new, folder=os.path.dirname(new))
            self._show_selected_path()

    def trash_selected(self):
        hit = self.current_hit()
        if not hit or not self._exists(hit):
            return
        answer = QMessageBox.question(self, "Move to trash", f"Move {hit.name} to the trash?",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        outcome = QFile.moveToTrash(hit.path)
        moved = outcome[0] if isinstance(outcome, tuple) else outcome
        if moved:
            self.model.remove_row(self.table.currentIndex().row())
        else:
            self._warn("Could not move it to the trash.")

    def _context_menu(self, position):
        if not self.table.indexAt(position).isValid():
            return
        menu = QMenu(self)
        for text, handler in (("Open with...", self.open_with),
                              ("Open containing folder", self.open_folder),
                              ("Copy path", self.copy_path),
                              ("Show nearby files", self.show_nearby),
                              (None, None),
                              ("Rename...", self.rename_selected),
                              ("Move to...", self.move_selected),
                              ("Move to trash", self.trash_selected)):
            if text is None:
                menu.addSeparator()
            else:
                menu.addAction(text).triggered.connect(handler)
        menu.exec(self.table.viewport().mapToGlobal(position))

    # Settings and index control

    def open_settings(self):
        dialog = SettingsDialog(self.cfg, parent=self)
        if dialog.exec() == QDialog.Accepted:
            self.cfg = dialog.result_config()
            save_config(self.cfg)
            self.indexer.cfg = self.cfg
            self.scope_box.setVisible(self.cfg.whole_system)
            if not self.cfg.whole_system:
                self.scope_box.setCurrentIndex(0)
            self.indexer.submit(("rescan",))

    def rebuild_index(self):
        answer = QMessageBox.question(
            self, "Rebuild index", "Forget the index and scan everything again?\n"
            "What you learned from your clicks is kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.indexer.submit(("rebuild",))
