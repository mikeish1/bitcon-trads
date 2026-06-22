"""
Safety tests: the web layer is read-only w.r.t. the trading tables, and the package
imports no order/executor surface. These are the load-bearing guarantees of the
whole design (architecture §9.1, §16).
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest_web import seed_sample_db
from web.db import ReadOnlyDB


def test_readonly_connection_blocks_writes(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)
    db = ReadOnlyDB(db_path)
    with db.conn() as c:
        # Reads work...
        assert c.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"] >= 3
        # ...writes to every trading table are rejected at the SQLite layer.
        for stmt in (
            "INSERT INTO trades(symbol,status) VALUES('XXX/USDT','OPEN')",
            "UPDATE state SET value='999' WHERE key='paper_cash'",
            "DELETE FROM decisions",
            "INSERT INTO decisions(ts,action) VALUES('now','BUY')",
        ):
            with pytest.raises(sqlite3.OperationalError):
                c.execute(stmt)


def test_web_package_imports_no_executor_or_broker():
    """The dashboard must never IMPORT an order-placing surface. We parse the AST of
    every web/ module and inspect real import statements (not comments/docstrings, so
    a module is free to *mention* what it deliberately avoids importing)."""
    import ast
    import pathlib

    # Module paths / names that carry an order surface and must never be imported.
    forbidden_modules = {"src.executor", "src.data_pipeline", "ccxt",
                         "src.etf.brokers.alpaca_broker", "src.etf.brokers.ccxt_broker"}
    forbidden_names = {"SpotExecutor", "build_exchange", "DataPipeline"}
    web_dir = pathlib.Path(__file__).resolve().parent.parent / "web"
    offenders: list[str] = []
    for path in web_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules or alias.name in forbidden_modules:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden_modules or mod.split(".")[0] == "ccxt":
                    offenders.append(f"{path.name}: from {mod}")
                for alias in node.names:
                    if alias.name in forbidden_names:
                        offenders.append(f"{path.name}: from {mod} import {alias.name}")
    assert not offenders, f"web package imports an order surface: {offenders}"


def test_query_only_pragma_is_set(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)
    db = ReadOnlyDB(db_path)
    with db.conn() as c:
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
