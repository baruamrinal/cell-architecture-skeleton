"""Fixtures shared by every test layer.

Adapter tests run against real services when they are available:
* ``POSTGRES_URL``: a Postgres login that may create databases, for example
  ``postgresql://cellarch:pw@127.0.0.1:5432/postgres``. Each test gets a fresh database.
* ``REDIS_URL``: see tests/acceptance/test_shared_rate_limiter.py.
Without them those tests are skipped; SQLite tests always run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from cellarch.common.sql import sqlite_url

BACKENDS = ["sqlite", "postgres"]


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """URL of a new, empty Postgres database, dropped after the test."""
    admin_url = os.environ.get("POSTGRES_URL")
    if not admin_url:
        pytest.skip("POSTGRES_URL not set: no Postgres available for adapter tests")
    import psycopg

    name = f"test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_database(admin_url, name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(params=BACKENDS)
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """A fresh database on each backend: SQLite always, Postgres when available."""
    if request.param == "sqlite":
        return sqlite_url(tmp_path / "tenant.db")
    url: str = request.getfixturevalue("postgres_url")
    return url
