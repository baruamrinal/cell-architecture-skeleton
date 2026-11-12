"""Cursor pagination (enterprise principle: stable APIs).

Lists return ``{"items": [...], "next_cursor": "..."}`` in creation order. The cursor is
opaque to clients: it encodes the position of the last item returned, so pages stay
stable while new records are added.
"""

import base64
import binascii
import json
from collections.abc import Sequence
from typing import Any

from cellarch.common.errors import ApiError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def encode_cursor(after_seq: int) -> str:
    raw = json.dumps({"after": after_seq}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        after = json.loads(raw)["after"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("invalid cursor")
    return after


def after_seq(cursor: str | None) -> int:
    """Position to list from, for an API request (400 on a bad cursor)."""
    if cursor is None:
        return 0
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise ApiError(400, "invalid_cursor", "the cursor is not valid") from exc


def page(rows: Sequence[Any], limit: int) -> tuple[list[Any], str | None]:
    """Rows fetched with ``LIMIT limit + 1``; returns this page and the next cursor."""
    items = list(rows[:limit])
    next_cursor = encode_cursor(int(items[-1]["seq"])) if len(rows) > limit else None
    return items, next_cursor
