"""Settings file and standard locations. Everything lives under your home folder."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

HOME = os.path.expanduser("~")

def _xdg(variable: str, fallback: str) -> str:
    return os.environ.get(variable) or os.path.join(HOME, fallback)

CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "filefinder")
DATA_DIR = os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), "filefinder")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "index.db")

# Folder names that are almost never useful to search.
DEFAULT_EXCLUDE_NAMES = ["node_modules", "__pycache__", "site-packages", "venv", ".venv", "lost+found"]

# Places on a Linux system that hold no real files (live kernel views, device nodes,
# packaged app images, caches). They are always skipped in whole computer mode.
ALWAYS_SKIP = ["/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/snapd", "/var/lib/docker",
               "/var/cache", "/var/lib/flatpak/repo"]

def is_under(path: str, root: str) -> bool:
    """True when path is root itself or lives inside it."""
    return root == "/" or path == root or path.startswith(root + "/")

def default_deep_folders() -> list:
    """Where files are read inside and watched live: your home folder and external drives."""
    return [HOME, "/media", "/mnt"]

@dataclass
class Config:
    whole_system: bool = True                     # search the whole computer
    include: list = field(default_factory=list)   # folders to index when whole_system is off
    deep_folders: list = field(default_factory=default_deep_folders)   # read inside files + watch live
    exclude: list = field(default_factory=list)   # folders to skip (with everything inside)
    exclude_names: list = field(default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES))
    skip_hidden: bool = True                      # names starting with a dot
    index_text: bool = True                       # read words inside text, Office, LibreOffice and e-book files
    index_photos: bool = True                     # read date taken and place from photos (EXIF)
    use_pdftotext: bool = False                   # also read PDFs (needs poppler-utils)
    always_ask_open: bool = False                 # ask which application to open files with, every time
    open_with: dict = field(default_factory=dict)  # remembered application per file type

def default_config() -> Config:
    """Search the whole computer. The folder list is only used if that is switched off,
    and starts with the usual personal folders that exist."""
    names = ["Documents", "Downloads", "Pictures", "Desktop", "Videos", "Music"]
    found = [os.path.join(HOME, n) for n in names if os.path.isdir(os.path.join(HOME, n))]
    return Config(include=found or [HOME])

def load_config() -> Config | None:
    """Return the saved settings, or None on first run (or if the file is broken)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        known = {f.name for f in fields(Config)}
        return Config(**{k: v for k, v in data.items() if k in known})   # new settings take their defaults
    except (OSError, ValueError, TypeError):
        return None

def save_config(cfg: Config) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, indent=2)
