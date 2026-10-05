"""Moving files, including to another drive, without ever leaving half a copy behind.

Same drive: one instant rename. Another drive: copy to a hidden temporary name next
to the destination, rename it into place, and only then remove the original. If
anything fails, the original is untouched and the temporary copy is removed.
Meant to run on a background thread so the window never freezes.
"""
from __future__ import annotations

import errno
import os
import shutil

def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)

def move_path(src: str, dst: str) -> None:
    """Move src to dst. Raises OSError (with a readable message) when it cannot."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "Something with that name already exists there")
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    _move_across_drives(src, dst)

def _move_across_drives(src: str, dst: str) -> None:
    is_folder = os.path.isdir(src) and not os.path.islink(src)
    if is_folder and (dst + "/").startswith(os.path.abspath(src) + "/"):
        raise OSError(errno.EINVAL, "A folder cannot be moved into itself")
    tmp = os.path.join(os.path.dirname(dst), f".filefinder-{os.getpid()}-{os.path.basename(dst)}.part")
    try:
        if is_folder:
            shutil.copytree(src, tmp, symlinks=True)
        else:
            shutil.copy2(src, tmp, follow_symlinks=False)
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "Something with that name already exists there")
        os.rename(tmp, dst)
    except BaseException:
        _remove(tmp)
        raise
    try:
        _remove_original(src, is_folder)
    except OSError as exc:
        raise OSError(exc.errno, f"Copied to the new place, but the original could not be removed "
                                 f"({exc.strerror}). You now have two copies.") from exc

def _remove_original(src: str, is_folder: bool) -> None:
    if is_folder:
        shutil.rmtree(src)
    else:
        os.unlink(src)
