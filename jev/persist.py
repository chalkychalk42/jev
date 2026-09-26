"""Atomic replacement for small durable control documents."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def input_lock_path() -> Path:
    """One physical input lease per user, shared across independent checkouts."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return root / "Jev" / "live-input.lock"
    return Path("/tmp") / f"jev-live-input-{os.getuid()}.lock"


# Windows over the WSL share refuses a rename onto a file that is open a moment longer:
# "Access is denied" from `choices.json`'s replace ended sessions 176 and 177, written
# about once a second by a hunt re-armed that often. The rename is tried again this many
# times, this far apart, before the error stands.
REPLACE_TRIES = 5
REPLACE_WAIT_S = 0.2


def atomic_json(path: Path, value: object) -> None:
    import time

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(REPLACE_TRIES):
            try:
                Path(temporary).replace(path)
                break
            except PermissionError:
                if attempt + 1 == REPLACE_TRIES:
                    raise
                time.sleep(REPLACE_WAIT_S)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """OS-owned lock: released on process death, with no stale PID guessing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import errno
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as exc:
                if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise BlockingIOError("lock is already held") from exc
                raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
