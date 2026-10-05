"""Find the applications that can open a file, and start one of them.

Every installed Linux application ships a small description file (a .desktop file)
that says what it is called, how to start it and which kinds of files it handles.
We read those, so the list matches what is really installed. An application is
started directly with the file as an argument, never through a shell, so unusual
characters in a file name cannot run anything.
"""
from __future__ import annotations

import configparser
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import quote

@dataclass
class App:
    id: str                       # the file name, for example org.kde.kate.desktop
    name: str
    exec_line: str
    icon: str = ""
    mime_types: set = field(default_factory=set)
    path: str = ""

def application_dirs() -> list[str]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    system = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs = [home] + system + ["/var/lib/flatpak/exports/share",
                              os.path.expanduser("~/.local/share/flatpak/exports/share"),
                              "/var/lib/snapd/desktop"]
    seen, out = set(), []
    for d in dirs:
        folder = os.path.join(d, "applications")
        if folder not in seen and os.path.isdir(folder):
            seen.add(folder)
            out.append(folder)
    return out

def _desktop_names() -> set[str]:
    return {n for n in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if n}

def _local_name(entry) -> str:
    """The application name in the user's language if there is one."""
    lang = os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    lang = lang.split(".")[0].split("@")[0]
    for key in (f"Name[{lang}]", f"Name[{lang.split('_')[0]}]", "Name"):
        if key and entry.get(key):
            return entry[key]
    return ""

def parse_desktop_file(path: str) -> App | None:
    """Read one .desktop file. Returns None for entries that should not be offered."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str                      # keys are case sensitive
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    if entry.get("Type") != "Application" or not entry.get("Exec"):
        return None
    flag = lambda key: entry.get(key, "false").strip().lower() == "true"
    if flag("NoDisplay") or flag("Hidden") or flag("Terminal"):
        return None                               # background helpers and terminal programs
    only, hidden_in = entry.get("OnlyShowIn", ""), entry.get("NotShowIn", "")
    desktops = _desktop_names()
    if desktops and only and not desktops & set(only.split(";")):
        return None
    if desktops and hidden_in and desktops & set(hidden_in.split(";")):
        return None
    try_exec = entry.get("TryExec")
    if try_exec and not (shutil.which(try_exec) or os.path.isfile(try_exec)):
        return None                               # listed but not actually installed
    mimes = {m for m in entry.get("MimeType", "").split(";") if m}
    name = _local_name(entry)
    if not name:
        return None
    return App(os.path.basename(path), name, entry["Exec"], entry.get("Icon", ""), mimes, path)

def load_apps(dirs: list[str] | None = None) -> dict[str, App]:
    """All offered applications by id. Earlier folders win, so your own copy beats the system's."""
    apps: dict[str, App] = {}
    for folder in dirs if dirs is not None else application_dirs():
        for path in sorted(glob.glob(os.path.join(folder, "*.desktop"))):
            app_id = os.path.basename(path)
            if app_id in apps:
                continue
            app = parse_desktop_file(path)
            if app:
                apps[app_id] = app
    return apps

def apps_for_mime(apps: dict[str, App], mime_names: list[str]) -> list[App]:
    """Applications that handle the file type, best match first.

    mime_names is the exact type followed by its more general parents, for example
    text/markdown, text/plain. An app made for the exact type comes before a general one.
    """
    ranked = []
    for app in apps.values():
        positions = [i for i, m in enumerate(mime_names) if m in app.mime_types]
        if positions:
            ranked.append((min(positions), app.name.lower(), app))
    return [app for _, _, app in sorted(ranked, key=lambda r: (r[0], r[1]))]

def system_default_id(mime: str) -> str | None:
    """The application the system would use for this file type (what a double click in
    other programs does), or None if it cannot be found."""
    tool = shutil.which("xdg-mime")
    if not tool:
        return None
    try:
        done = subprocess.run([tool, "query", "default", mime], capture_output=True,
                              text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None

_CODE = re.compile(r"%(.)")

def build_command(app: App, target: str) -> list[str]:
    """The argument list that starts the app with the file, following the desktop entry rules."""
    uri = "file://" + quote(target, safe="/")
    tokens = shlex.split(app.exec_line)           # raises ValueError for broken quoting
    used_file = False

    def expand(match):
        nonlocal used_file
        code = match.group(1)
        if code in "fF":
            used_file = True
            return target
        if code in "uU":
            used_file = True
            return uri
        return {"%": "%", "c": app.name, "k": app.path}.get(code, "")

    command = []
    for token in tokens:
        if token in ("%i",):                      # icon option, not needed
            continue
        new = _CODE.sub(expand, token)
        if new or not _CODE.search(token):
            command.append(new)
    if not used_file:
        command.append(target)                    # the app named no place for the file
    return command

LIBRARY_VARIABLES = ("LD_LIBRARY_PATH", "LD_PRELOAD")
BUNDLE_VARIABLES = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")

def clean_env() -> dict:
    """The environment for starting other programs.

    A built copy of this app points the library search path at its own bundled libraries.
    Programs started from it would load those instead of their own and often crash without
    a word. Here the original values are put back. Outside a built copy nothing changes.
    """
    env = dict(os.environ)
    bundle = getattr(sys, "_MEIPASS", "")
    if not getattr(sys, "frozen", False) or not bundle:
        return env
    for key in LIBRARY_VARIABLES:
        original = env.pop(key + "_ORIG", None)
        if original is None:
            env.pop(key, None)
        else:
            env[key] = original
    for key in BUNDLE_VARIABLES:
        if env.get(key, "").startswith(bundle):
            del env[key]
    return env

def launch(app: App, target: str) -> subprocess.Popen:
    """Start the application with the file. Raises OSError or ValueError if it cannot."""
    return subprocess.Popen(build_command(app, target), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, cwd=os.path.expanduser("~"), env=clean_env())

def open_default(target: str, tools: tuple = ("gio", "xdg-open")) -> bool:
    """Open a file or folder with the application the system has chosen for its type.

    Tries gio, then xdg-open. A tool that is still running after a few seconds has handed
    the file to an application, which counts as success. Returns False when nothing could
    open it, so the caller can tell the person instead of doing nothing.
    """
    env = clean_env()
    for name in tools:
        found = shutil.which(name)
        if not found:
            continue
        command = [found, "open", target] if name == "gio" else [found, target]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       cwd=os.path.expanduser("~"), env=env)
            if process.wait(timeout=3) == 0:
                return True
        except subprocess.TimeoutExpired:
            return True
        except OSError:
            continue
    return False
