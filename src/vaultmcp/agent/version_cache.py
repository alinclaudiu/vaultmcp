"""SQLite cache of the last-known version per page on this agent.

Lets us send accurate ``base_version`` on writes (without re-fetching from
master every time) and resume the SSE stream from the right
``global_version`` after a restart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VersionCache:
    db_path: Path

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS page_versions (
                    path TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    global_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cursor (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_global_version INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO cursor (id, last_global_version) VALUES (1, 0);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_version(self, path: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM page_versions WHERE path = ?", (path,)
            ).fetchone()
        return row[0] if row else None

    def set_version(self, path: str, version: int, global_version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO page_versions (path, version, global_version)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    version = excluded.version,
                    global_version = excluded.global_version
                """,
                (path, version, global_version),
            )

    def get_cursor(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_global_version FROM cursor WHERE id = 1"
            ).fetchone()
        return row[0] if row else 0

    def set_cursor(self, global_version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE cursor SET last_global_version = ? WHERE id = 1",
                (global_version,),
            )
