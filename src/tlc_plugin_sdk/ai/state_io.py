# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Private, atomic state files with collision-free owner names."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def owner_key(user: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,120}", user):
        return user
    return "sha256-" + hashlib.sha256(user.encode()).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
