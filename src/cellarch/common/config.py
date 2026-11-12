"""Helpers for reading configuration from environment variables."""

import os


class ConfigError(ValueError):
    """Configuration is missing or malformed."""


def parse_pairs(raw: str) -> dict[str, str]:
    """Parse ``"a=1, b=2"`` into ``{"a": "1", "b": "2"}``."""
    pairs: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        key, separator, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or not value:
            raise ConfigError(f"malformed entry {part!r}; expected key=value")
        pairs[key] = value
    return pairs


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is required")
    return value
