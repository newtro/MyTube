"""Postgres connection helper and a minimal ordered-SQL migration runner."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


def connect(database_url: str) -> psycopg.Connection:
    """Open a connection with dict rows and the pgvector adapter registered."""
    conn = psycopg.connect(database_url, row_factory=dict_row)
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # vector extension not installed yet — `mytube db migrate` creates it.
        pass
    return conn


def migrations_dir() -> Path:
    return Path(str(importlib.resources.files("mytube") / "migrations"))


def applied_migrations(conn: psycopg.Connection) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r["filename"] for r in rows}

def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending ``mytube/migrations/*.sql`` in filename order.

    Each migration runs in its own transaction together with the bookkeeping
    row, so a failure leaves the database at the previous migration boundary.

    RULE: a migration file must NEVER be edited once any environment has
    recorded it in schema_migrations — ship a new numbered file instead.
    (0001 was edited during Phase 0 development before any database ever
    ran it; that was the last permissible moment.)
    """
    done = applied_migrations(conn)
    conn.commit()
    applied: list[str] = []
    for path in sorted(migrations_dir().glob("*.sql")):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            conn.commit()
        except psycopg.errors.InsufficientPrivilege as exc:
            conn.rollback()
            raise RuntimeError(
                f"migration {path.name} failed with a privilege error: {exc}. "
                "If this is CREATE EXTENSION vector, run once as superuser: "
                '  psql -d mytube -c "CREATE EXTENSION vector;"'
            ) from exc
        except Exception:
            conn.rollback()
            raise
        # the vector type may have just been created — (re)register the adapter
        try:
            register_vector(conn)
        except psycopg.ProgrammingError:
            pass
        applied.append(path.name)
    return applied
