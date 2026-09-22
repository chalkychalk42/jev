"""Choose durable runtime storage whose operating system supports our file locks."""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import sys
from pathlib import Path, PureWindowsPath

_WINDOWS = sys.platform == "win32"


def default_learning_store(root: Path) -> Path:
    """Keep Windows UNC checkouts' mutable learning state on a native drive.

    Windows byte-range locking is unavailable on the WSL file share. A native store
    keeps the worker, registry and teacher budget together without weakening their
    locks. The checkout identity makes this location stable across process restarts.
    Explicit CLI storage choices remain the caller's responsibility.
    """
    root = Path(root)
    windows_root = PureWindowsPath(root)
    if not _WINDOWS or not windows_root.drive.startswith("\\\\"):
        return root / "var" / "learning"

    local = os.environ.get("LOCALAPPDATA")
    local_path = PureWindowsPath(local or "")
    if (not local_path.is_absolute() or local_path.drive.startswith("\\\\")
            or not re.fullmatch(r"[A-Za-z]:", local_path.drive)):
        raise ValueError("Windows UNC checkouts require LOCALAPPDATA on a native drive; "
                         "set LOCALAPPDATA or explicitly select a native learning store")

    drive, tail = ntpath.splitdrive(ntpath.normpath(str(windows_root)))
    drive = ntpath.normcase(drive)
    # The server/share names are case-insensitive aliases, but WSL directories are
    # case-sensitive. Folding the whole path merged project/ and Project/ stores.
    if drive.startswith("\\\\wsl$\\"):
        drive = "\\\\wsl.localhost\\" + drive[len("\\\\wsl$\\"):]
    identity = drive + tail
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    project = re.sub(r"[^a-z0-9._-]+", "-", windows_root.name.lower()).strip(".-")
    directory = f"{project or 'project'}-{digest}"
    return Path(local) / "Jev" / "learning" / directory
