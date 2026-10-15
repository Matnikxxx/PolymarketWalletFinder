"""
Atomic JSON file persistence (spec section 27). Writes go to a temp file
in the same directory and are atomically renamed into place, so a crash or
restart mid-write can never leave a corrupt/half-written state file.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


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
