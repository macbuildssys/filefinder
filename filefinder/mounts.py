"""Find mounted places that must not be crawled.

Live kernel views (/proc, /sys), device nodes, memory disks, packaged app images
(squashfs) and network drives either hold no real files or could make a scan hang.
Normal disks and USB sticks are kept.
"""
from __future__ import annotations

SKIP_TYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2", "securityfs",
    "debugfs", "tracefs", "configfs", "fusectl", "pstore", "bpf", "mqueue", "hugetlbfs",
    "autofs", "binfmt_misc", "overlay", "squashfs", "efivarfs", "nsfs", "rpc_pipefs",
    "fuse.portal", "fuse.gvfsd-fuse", "fuse.snapfuse",
    # network drives: slow, can hang, and not "this computer"
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "ceph", "glusterfs", "afs",
    "fuse.sshfs", "fuse.rclone", "fuse.davfs", "davfs",
}

def _unescape(text: str) -> str:
    """mountinfo writes spaces and tabs as octal codes such as \\040."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and text[i + 1:i + 4].isdigit():
            out.append(chr(int(text[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(text[i])
            i += 1
    return "".join(out)

def parse_mountinfo(text: str) -> set[str]:
    """Mount points to skip, from the text of /proc/self/mountinfo. The root is never skipped."""
    skip = set()
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields, rest = left.split(), right.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        mount_point, fs_type = _unescape(fields[4]), rest[0]
        if mount_point != "/" and fs_type in SKIP_TYPES:
            skip.add(mount_point)
    return skip

def skipped_mount_points() -> set[str]:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            return parse_mountinfo(fh.read())
    except OSError:
        return set()
