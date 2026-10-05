"""File types and safe reading of text.

Files are only ever read, never opened by another program or run.
Documents are never handed to an XML parser or a document library: we cut the
text out with plain string handling and hard size limits, so crafted files
(zip bombs, XML tricks) have nothing to exploit.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import stat
import subprocess
import zipfile
import zlib

KIND_EXTENSIONS = {
    "pdf": {"pdf"},
    "image": {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "svg", "tif", "tiff",
              "raw", "cr2", "nef", "arw", "orf", "rw2", "dng", "avif"},
    "video": {"mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v"},
    "audio": {"mp3", "flac", "wav", "ogg", "m4a", "opus", "aac"},
    "document": {"doc", "docx", "odt", "rtf", "txt", "md", "epub", "tex", "rst"},
    "spreadsheet": {"xls", "xlsx", "ods", "csv", "tsv"},
    "presentation": {"ppt", "pptx", "odp"},
    "archive": {"zip", "tar", "gz", "xz", "bz2", "7z", "rar", "zst", "tgz"},
    "code": {"py", "js", "ts", "c", "h", "cpp", "rs", "go", "java", "sh", "html", "css",
             "json", "yaml", "yml", "toml", "sql", "php", "rb"},
}
_KIND_BY_EXT = {ext: kind for kind, exts in KIND_EXTENSIONS.items() for ext in exts}

# Plain text formats whose words we read for the content search.
TEXT_EXTENSIONS = {
    "txt", "md", "rst", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "cfg", "conf",
    "log", "py", "js", "ts", "c", "h", "cpp", "rs", "go", "java", "sh", "html", "css",
    "xml", "tex", "sql", "org", "php", "rb",
}
# Word, Excel, PowerPoint (new), LibreOffice and e-books are zip files holding XML.
ZIP_DOC_EXTENSIONS = {"docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"}
# Old binary Office files: read roughly, and only if the optional olefile package is installed.
OLE_EXTENSIONS = {"doc", "xls", "ppt"}
RTF_EXTENSIONS = {"rtf"}
PHOTO_EXTENSIONS = {"jpg", "jpeg", "tif", "tiff", "png", "webp", "heic", "heif", "avif",
                    "dng", "cr2", "nef", "arw", "orf", "rw2"}

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024    # skip huge text files
MAX_ZIP_FILE_BYTES = 50 * 1024 * 1024    # skip huge zip documents
MAX_MEMBER_BYTES = 600 * 1024            # read at most this much from one part of a zip
MAX_ZIP_MEMBERS = 40                     # and look at no more parts than this
MAX_CONTENT_CHARS = 20000                # index at most this much text per file

_TAGS = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")

def split_ext(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""

def kind_of(ext: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    return _KIND_BY_EXT.get(ext, "file")

def type_label(kind: str, ext: str) -> str:
    if kind == "folder":
        return "Folder"
    if kind == "pdf":
        return "PDF"
    if kind == "file":
        return ext.upper() if ext else "File"
    return f"{kind.title()} ({ext})" if ext else kind.title()

def text_extensions(use_pdftotext: bool) -> set:
    """All extensions whose text we can read with the current settings."""
    exts = TEXT_EXTENSIONS | ZIP_DOC_EXTENSIONS | RTF_EXTENSIONS
    if _ole_available():
        exts = exts | OLE_EXTENSIONS
    if use_pdftotext:
        exts = exts | {"pdf"}
    return exts

def open_regular(path: str) -> int | None:
    """Open a regular file for reading and return the descriptor, or None.

    O_NOFOLLOW and O_NONBLOCK plus the regular file check make sure a file swapped for
    a link or a named pipe cannot make us read something else or wait forever.
    The caller closes the descriptor.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd

def read_text(path: str, ext: str, size: int, use_pdftotext: bool = False) -> str:
    """Return up to MAX_CONTENT_CHARS of text from a file, or '' if there is none."""
    try:
        if ext == "pdf":
            return _read_pdf(path) if use_pdftotext else ""
        if ext in ZIP_DOC_EXTENSIONS:
            return _read_zip_document(path, ext, size)
        if ext in OLE_EXTENSIONS:
            return _read_ole(path, ext, size)
        if ext in RTF_EXTENSIONS:
            return _strip_rtf(_read_head(path, 200 * 1024))
        if ext in TEXT_EXTENSIONS and size <= MAX_TEXT_FILE_BYTES:
            data = _read_head(path, MAX_CONTENT_CHARS * 2)
            if b"\x00" in data:   # binary data pretending to be text
                return ""
            return data.decode("utf-8", "ignore")[:MAX_CONTENT_CHARS]
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError,
            KeyError, zipfile.BadZipFile, zlib.error):
        return ""
    return ""

def _read_head(path: str, limit: int) -> bytes:
    fd = open_regular(path)
    if fd is None:
        return b""
    try:
        return os.read(fd, limit)
    finally:
        os.close(fd)

def _clean(markup: str) -> str:
    """Remove tags and entities, squeeze spaces."""
    text = html.unescape(_TAGS.sub(" ", markup))
    return _SPACES.sub(" ", text).strip()

def _wanted_members(zf: zipfile.ZipFile, ext: str) -> list:
    names = [i for i in zf.infolist()[:5000] if not i.is_dir()]
    by_name = {i.filename: i for i in names}
    if ext == "docx":
        return [by_name[n] for n in ("word/document.xml",) if n in by_name]
    if ext == "xlsx":
        return [by_name[n] for n in ("xl/sharedStrings.xml",) if n in by_name]
    if ext == "pptx":
        slides = sorted(n for n in by_name if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        return [by_name[n] for n in slides[:MAX_ZIP_MEMBERS]]
    if ext in ("odt", "ods", "odp"):
        return [by_name[n] for n in ("content.xml",) if n in by_name]
    # epub: the chapters are xhtml files
    chapters = sorted(n for n in by_name if n.lower().endswith((".xhtml", ".html", ".htm")))
    return [by_name[n] for n in chapters[:MAX_ZIP_MEMBERS]]

def _read_zip_document(path: str, ext: str, size: int) -> str:
    if size > MAX_ZIP_FILE_BYTES:
        return ""
    pieces, total = [], 0
    with zipfile.ZipFile(path) as zf:
        for member in _wanted_members(zf, ext):
            if member.flag_bits & 0x1:   # encrypted
                continue
            with zf.open(member) as part:
                chunk = part.read(MAX_MEMBER_BYTES)   # never more than this, whatever the zip claims
            text = _clean(chunk.decode("utf-8", "ignore"))
            pieces.append(text)
            total += len(text)
            if total >= MAX_CONTENT_CHARS:
                break
    return " ".join(pieces)[:MAX_CONTENT_CHARS]

def _strip_rtf(raw: bytes) -> str:
    text = raw.decode("latin-1", "ignore")
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = re.sub(r"[{}\\]", " ", text)
    return _SPACES.sub(" ", text).strip()[:MAX_CONTENT_CHARS]

def _ole_available() -> bool:
    try:
        import olefile  # noqa: F401
        return True
    except ImportError:
        return False

def _rough_strings(data: bytes) -> str:
    """Pull readable runs out of binary data (old .doc, .xls, .ppt files).

    This is deliberately rough: it keeps stretches that look like words and drops
    the rest. Good enough to find a file by what it says, not to display it.
    """
    runs = [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{6,}", data)]
    runs += [m.decode("utf-16-le", "ignore") for m in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", data)]
    keep = [r for r in runs if sum(c.isalpha() or c == " " for c in r) >= 0.8 * len(r)]
    return _SPACES.sub(" ", " ".join(keep))[:MAX_CONTENT_CHARS]

_OLE_STREAMS = {"doc": ("WordDocument",), "xls": ("Workbook", "Book"), "ppt": ("PowerPoint Document",)}

def _read_ole(path: str, ext: str, size: int) -> str:
    if size > MAX_ZIP_FILE_BYTES:
        return ""
    try:
        import olefile
    except ImportError:
        return ""
    # olefile is a third party parser reading untrusted data and can fail in many ways
    # on damaged files. Whatever goes wrong, the file simply counts as unreadable.
    try:
        ole = olefile.OleFileIO(path)
    except Exception:
        return ""
    try:
        for name in _OLE_STREAMS[ext]:
            if ole.exists(name):
                return _rough_strings(ole.openstream(name).read(2 * 1024 * 1024))
    except Exception:
        return ""
    finally:
        ole.close()
    return ""

def _read_pdf(path: str) -> str:
    tool = shutil.which("pdftotext")
    if not tool:
        return ""
    try:
        done = subprocess.run(
            [tool, "-l", "3", "-nopgbrk", "-q", path, "-"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "ignore")[:MAX_CONTENT_CHARS]
