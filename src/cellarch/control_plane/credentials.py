"""Cell credentials: the secret the control plane presents to each cell's /internal API.

Credentials are write-only: they can be stored and used, never returned by an API.

``LocalCredentialStore`` (local development) keeps credentials registered through the
API in the control plane's own database, with credentials from configuration
(``CELL_CREDENTIALS``) as a fallback. On AWS an SSM Parameter Store implementation
(SecureString, KMS-encrypted) replaces it behind the same interface (docs/plan.md §8).
"""

from collections.abc import Mapping
from typing import Protocol

from cellarch.common.db import connect, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS cell_credentials (
    cell_id    TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class CellCredentials(Protocol):
    def get(self, cell_id: str, /) -> str | None: ...
    def put(self, cell_id: str, token: str) -> None: ...
    def delete(self, cell_id: str) -> None: ...


class LocalCredentialStore:
    def __init__(self, db_path: str, static: Mapping[str, str]) -> None:
        self._db_path = db_path
        self._static = dict(static)
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)

    def get(self, cell_id: str, /) -> str | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT token FROM cell_credentials WHERE cell_id = ?", (cell_id,)
            ).fetchone()
        return str(row["token"]) if row else self._static.get(cell_id)

    def put(self, cell_id: str, token: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cell_credentials (cell_id, token, updated_at) "
                "VALUES (?, ?, ?)",
                (cell_id, token, utcnow()),
            )

    def delete(self, cell_id: str) -> None:
        """Remove a registered credential. Credentials from configuration stay."""
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM cell_credentials WHERE cell_id = ?", (cell_id,))
