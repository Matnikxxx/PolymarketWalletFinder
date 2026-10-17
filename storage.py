"""
Atomic JSON file persistence (spec section 27). Writes go to a temp file
in the same directory and are atomically renamed into place, so a crash or
restart mid-write can never leave a corrupt/half-written state file.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class WalletStateError(RuntimeError):
    """
    The wallet-state file exists but cannot be safely interpreted.

    Raised instead of returning an empty list, because "unreadable" and
    "no wallets yet" are very different states: treating the former as the
    latter would make the next scan write a fresh file over the user's
    tracked flags, notes and scan history.
    """

    def __init__(
        self,
        path: Path,
        reason: str,
        backup_path: Optional[Path] = None,
        backup_error: Optional[str] = None,
    ):
        self.path = Path(path)
        self.reason = reason
        self.backup_path = Path(backup_path) if backup_path is not None else None
        self.backup_error = backup_error

        message = (
            f"Wallet state file {self.path} could not be loaded: {reason}. "
            "The original file was NOT modified."
        )
        if self.backup_path is not None:
            message += f" A recoverable copy was saved to {self.backup_path}."
        elif backup_error:
            message += (
                f" A backup copy could not be created ({backup_error}); "
                "the original file is still intact."
            )
        else:
            message += " No backup could be created because the file could not be read."
        message += " Fix or restore the file, then re-run."
        super().__init__(message)


def load_json(path: Path, default: Any):
    if not Path(path).exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return default


def _preserve_state_copy(path: Path, raw: bytes) -> Path:
    """Write a durable, byte-identical copy of the unreadable state file.

    A repeated read of the same corrupt file (e.g. `--report` run twice)
    reuses the identical existing backup instead of littering the directory.

    Permissions
    -----------
    POSIX (Linux/macOS): the copy is created with mode ``0600`` (owner-only),
    further restricted by the process umask -- a backup can never be more
    permissive than the owner. This matters because wallet records contain
    the user's tracked addresses and free-text notes.

    Windows: ``os.open``'s mode bits do not map to NTFS ACLs; the backup
    inherits the ACL of its parent directory exactly like any other file.
    ``0o600`` is still passed to keep the intent explicit and for
    consistency across platforms. Do not rely on it as a Windows security
    boundary -- protect the folder instead.

    OneDrive / cloud-synced folders: the source may be a dehydrated
    placeholder. If the provider cannot materialise it, ``read_bytes``
    raises ``OSError``; the loader reports that (no backup exists to write)
    and the original file is left untouched. If the backup write itself
    fails (quota, sync lock, permission), the partial copy is removed and
    the error reports the failure -- the original is never modified.
    """
    for candidate in sorted(path.parent.glob(f"{path.name}.corrupt-*.bak"), reverse=True):
        try:
            if candidate.read_bytes() == raw:
                return candidate
        except OSError:
            continue

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}.bak")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}.bak")
        counter += 1

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        stream = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise

    try:
        with stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # A half-written backup is worse than none: remove it so a later
        # recovery does not restore truncated bytes. The source file was
        # only ever read, so it remains byte-identical either way.
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return target


def _try_preserve_state(path: Path, raw: bytes) -> tuple[Optional[Path], Optional[str]]:
    try:
        return _preserve_state_copy(path, raw), None
    except OSError as exc:
        return None, str(exc)


def _state_error(path: Path, reason: str, raw: Optional[bytes]) -> WalletStateError:
    backup = None
    backup_error = None
    if raw is not None:
        backup, backup_error = _try_preserve_state(path, raw)
    return WalletStateError(path, reason, backup, backup_error)


def load_wallet_state(path: Path) -> list[dict]:
    """Load the wallet-state file, refusing to guess when it is unreadable.

    A missing file is a legitimate first run and yields ``[]``. Anything
    else -- invalid JSON, an empty file, a non-array root, non-object
    entries, or an I/O error -- raises :class:`WalletStateError` after
    preserving a byte-identical backup. The original is never modified.
    """
    path = Path(path)
    if not path.exists():
        return []

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _state_error(path, f"the file could not be read ({exc})", None) from exc

    if not raw.strip():
        raise _state_error(path, "the file is empty", raw)

    try:
        data = json.loads(raw)
    except UnicodeDecodeError as exc:
        raise _state_error(path, f"the file is not valid UTF-8 text ({exc})", raw) from exc
    except json.JSONDecodeError as exc:
        raise _state_error(
            path, f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}", raw
        ) from exc

    if not isinstance(data, list):
        raise _state_error(
            path,
            f"expected a JSON array of wallet records, found {type(data).__name__}",
            raw,
        )

    bad_indexes = [i for i, item in enumerate(data) if not isinstance(item, dict)]
    if bad_indexes:
        shown = ", ".join(str(i) for i in bad_indexes[:5])
        extra = "" if len(bad_indexes) <= 5 else f" (+{len(bad_indexes) - 5} more)"
        noun = "entry is" if len(bad_indexes) == 1 else "entries are"
        raise _state_error(
            path,
            f"{len(bad_indexes)} {noun} not a JSON object (index {shown}{extra})",
            raw,
        )

    return data


def save_json_atomic(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
