"""Small, dependency-free loader for the backend's ignored .env file."""

from __future__ import annotations

import os
from pathlib import Path


def load_environment(path: str | Path | None = None) -> Path | None:
    """Load backend/.env without overriding variables already in the process."""
    env_path = Path(path) if path is not None else Path(__file__).with_name(".env")
    if not env_path.is_file():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return env_path
