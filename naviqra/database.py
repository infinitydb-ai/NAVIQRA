"""Small DB-API adapter for SQLite development and PostgreSQL production.

The stores use a deliberately small SQL subset shared by both backends.
PostgreSQL uses psycopg 3 and dict rows; SQLite remains dependency-free.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator


class DatabaseConnection:
    def __init__(self, connection: Any, dialect: str, tenant: str | None = None):
        self.raw = connection
        self.dialect = dialect
        self.tenant = tenant

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()):
        if self.dialect == "postgresql":
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self.raw.executescript(script)
            return
        statements = []
        current = []
        in_dollar = False
        i = 0
        n = len(script)
        while i < n:
            if script[i:i+2] == "$$":
                in_dollar = not in_dollar
                current.append("$$")
                i += 2
                continue
            if script[i] == ";" and not in_dollar:
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 1
                continue
            current.append(script[i])
            i += 1
        stmt = "".join(current).strip()
        if stmt:
            statements.append(stmt)

        for statement in statements:
            self.execute(statement)

    def __enter__(self):
        if hasattr(self.raw, "__enter__"):
            self.raw.__enter__()
        if self.dialect == "postgresql":
            val = self.tenant if self.tenant is not None else ""
            self.execute("SELECT set_config('app.current_tenant', %s, true)", (val,))
        return self

    def close(self) -> None:
        if hasattr(self.raw, "close") and callable(self.raw.close):
            self.raw.close()

    def __exit__(self, exc_type, exc, tb):
        try:
            if hasattr(self.raw, "__exit__"):
                return self.raw.__exit__(exc_type, exc, tb)
        finally:
            self.close()

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.dialect = "postgresql" if dsn.startswith(("postgres://", "postgresql://")) else "sqlite"
        if self.dialect == "postgresql":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("PostgreSQL requires psycopg[binary]>=3.2") from exc
            self._connect_impl = lambda: psycopg.connect(self.dsn, row_factory=dict_row)
        else:
            path = dsn.removeprefix("sqlite:///") if dsn.startswith("sqlite:///") else dsn
            self.path = path
            self._connect_impl = lambda: sqlite3.connect(path, timeout=20, check_same_thread=False)

    def connect(self, tenant: str | None = None) -> DatabaseConnection:
        raw = self._connect_impl()
        if self.dialect == "sqlite":
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
        return DatabaseConnection(raw, self.dialect, tenant=tenant)

    def initialize_script(self, script: str) -> None:
        if self.dialect == "sqlite":
            with self.connect() as db:
                db.executescript(script)
        else:
            with self.connect() as db:
                db.executescript(script)

    def check(self) -> None:
        with self.connect() as db:
            db.execute("SELECT 1").fetchone()
