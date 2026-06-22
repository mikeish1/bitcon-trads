"""
Read-only SQLite access layer for the dashboard.

Why this design (see docs/DASHBOARD_ARCHITECTURE.md §5.2):
  * The bot owns the single WRITER connection to `trading_state.db` (WAL mode).
  * This module opens the SAME file with a READ-ONLY URI (`mode=ro`). SQLite then
    rejects any write at the C layer (`SQLITE_READONLY`), so a bug here can never
    corrupt or block the trading tables. `PRAGMA query_only=ON` is belt-and-braces.
  * WAL lets unlimited readers run concurrently with the one writer without ever
    causing "database is locked" for the bot. A short `busy_timeout` covers the
    rare WAL-checkpoint window.
  * Connections are short-lived and created per unit-of-work. SQLite connects are
    cheap, and per-request connections sidestep thread-affinity entirely (FastAPI
    runs sync endpoints in a threadpool).

The sampler (web/snapshots.py) is the ONE component that needs a read-write
connection, and only to its own isolated `equity_snapshots` table; it uses
`rw_conn()` here, never the trading tables.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger


class ReadOnlyDB:
    """Factory for short-lived, read-only SQLite connections to the bot's DB."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # A real file path -> read-only URI. `:memory:` (used only in tests) cannot
        # be opened read-only across connections, so we special-case it.
        self._is_memory = db_path == ":memory:"
        if self._is_memory:
            self._uri = "file::memory:?cache=shared"
        else:
            # as_posix() so the URI is well-formed on Windows too (forward slashes).
            self._uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        logger.debug("ReadOnlyDB configured for {} (uri={}).", db_path, self._uri)

    @property
    def path(self) -> str:
        return self._db_path

    def exists(self) -> bool:
        return self._is_memory or Path(self._db_path).exists()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a read-only connection with `Row` access. Closed on exit.

        Raises sqlite3.OperationalError if the DB file does not exist yet (the bot
        creates it on first run); callers should surface that as a clear 503.
        """
        c = sqlite3.connect(self._uri, uri=True, check_same_thread=False, timeout=5.0)
        c.row_factory = sqlite3.Row
        try:
            # Enforce read-only at the statement layer as well as the open mode.
            if not self._is_memory:
                c.execute("PRAGMA query_only=ON")
            c.execute("PRAGMA busy_timeout=5000")
            yield c
        finally:
            c.close()

    def table_exists(self, name: str) -> bool:
        """True if `name` exists. Used to degrade gracefully when the sampler has
        not yet created `equity_snapshots`, or the bot has not yet created its
        tables on a brand-new deploy."""
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                return row is not None
        except sqlite3.OperationalError:
            return False


@contextmanager
def rw_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """Read-WRITE connection used ONLY by the equity-snapshots sampler, and ONLY
    to touch the isolated `equity_snapshots` table. It opens with the same WAL +
    busy-timeout pragmas the bot uses so it shares the file cleanly.

    This is the single place in the whole package allowed to write, and it must
    never issue DML against `state`, `trades`, or `decisions`.
    """
    c = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
    c.row_factory = sqlite3.Row
    try:
        if db_path != ":memory:":
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                pass
        yield c
    finally:
        c.close()
