""".env file support, shared by cellctl and scripts/dev.py (`make up`).

Standard library only: scripts/dev.py imports it before the virtual environment exists.
"""

import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines with # comments, an optional `export` prefix and quotes."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise ValueError(f".env line {number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str] | None:
    """Values from ``path``, or None when the file doesn't exist."""
    if not path.is_file():
        return None
    return parse_env(path.read_text(encoding="utf-8"))
