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
    def __init__(self, connection: Any, dialect: str):
        self.raw = connection
        self.dialect = dialect

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()):
        if self.dialect == "postgresql":
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self.raw.executescript(script)
            return
        # Schema scripts in this project contain no procedural blocks or semicolons
        # embedded in string literals.
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def __enter__(self):
        if hasattr(self.raw, "__enter__"):
            self.raw.__enter__()
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

    def connect(self) -> DatabaseConnection:
        raw = self._connect_impl()
        if self.dialect == "sqlite":
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
        return DatabaseConnection(raw, self.dialect)

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
