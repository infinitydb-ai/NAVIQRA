import json
import os
from pathlib import Path
import sqlite3
import tempfile
import pytest

from naviqra.database import Database, DatabaseConnection
from naviqra.platform.core import PlatformStore


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return self


def test_sqlite_database_health(tmp_path):
    database = Database(str(tmp_path / "db.sqlite"))
    with database.connect() as conn:
        conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sample VALUES (?)", ("one",))
        row = conn.execute("SELECT id FROM sample").fetchone()
    assert row["id"] == "one"
    database.check()


def test_postgres_placeholder_translation_and_schema_split():
    raw = RecordingConnection()
    conn = DatabaseConnection(raw, "postgresql")
    conn.execute("SELECT * FROM sample WHERE id=? AND tenant=?", ("a", "b"))
    conn.executescript("CREATE TABLE one (id TEXT); CREATE TABLE two (id TEXT);")
    assert raw.statements[0] == (
        "SELECT * FROM sample WHERE id=%s AND tenant=%s", ("a", "b")
    )
    assert raw.statements[1][0] == "CREATE TABLE one (id TEXT)"
    assert raw.statements[2][0] == "CREATE TABLE two (id TEXT)"


def test_sqlite_connection_closed_on_success(tmp_path):
    database = Database(str(tmp_path / "closed_success.sqlite"))
    with database.connect() as conn:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO items VALUES (?, ?)", ("k1", "v1"))
        raw = conn.raw

    # The raw connection must be closed upon exiting context manager
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        raw.execute("SELECT 1")

    # Transaction committed: verify persistence with a fresh connection
    with database.connect() as check_conn:
        row = check_conn.execute("SELECT val FROM items WHERE id=?", ("k1",)).fetchone()
        assert row["val"] == "v1"


def test_sqlite_connection_closed_on_exception(tmp_path):
    database = Database(str(tmp_path / "closed_exception.sqlite"))
    with database.connect() as init_conn:
        init_conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, val TEXT)")

    with pytest.raises(RuntimeError, match="simulated failure"):
        with database.connect() as conn:
            conn.execute("INSERT INTO items VALUES (?, ?)", ("k2", "v2"))
            raw = conn.raw
            raise RuntimeError("simulated failure")

    # The raw connection must be closed even when exception is raised
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        raw.execute("SELECT 1")

    # Transaction rolled back: verify item was not persisted
    with database.connect() as check_conn:
        row = check_conn.execute("SELECT val FROM items WHERE id=?", ("k2",)).fetchone()
        assert row is None


def test_windows_temporary_database_directory_cleanup():
    temp_dir_path = None
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir_path = tmp
        store = PlatformStore(Path(tmp) / "platform_test.sqlite")
        store.put_memory("tenant-1", "traveler-1", "profile", "style", "fast")
        node_a = store.add_node("tenant-1", "place", "Tokyo", {})
        node_b = store.add_node("tenant-1", "place", "Kyoto", {})
        store.add_edge("tenant-1", node_a["id"], "near", node_b["id"])
        store.vote("tenant-1", "g1", "m1", "c1", 0.9)
        store.audit("tenant-1", "test", "write", "memory", "1")
        assert len(store.get_memories("tenant-1", "traveler-1")) == 1

    # On Windows, if SQLite connections were leaked, rmtree would fail with WinError 32.
    # Deterministic closure ensures the directory is removed completely upon context exit.
    assert temp_dir_path is not None
    assert not os.path.exists(temp_dir_path)


def test_benchmark_output_filename_and_historical_isolation():
    root = Path(__file__).resolve().parents[1]

    # 1. Verify benchmarks/v05_benchmark.py targets benchmark-results-v0.5.0.json
    v05_source = (root / "benchmarks" / "v05_benchmark.py").read_text(encoding="utf-8")
    assert 'benchmark-results-v0.5.0.json' in v05_source
    assert 'benchmark-results-v0.4.1.json' not in v05_source

    # 2. Verify benchmarks/v04_benchmark.py targets benchmark-results-v0.4.1.json
    v04_source = (root / "benchmarks" / "v04_benchmark.py").read_text(encoding="utf-8")
    assert 'benchmark-results-v0.4.1.json' in v04_source

    # 3. Protect historical benchmark files: verify they exist, are valid JSON, and have expected structure
    v03_file = root / "benchmark-results-v0.3.0.json"
    v04_file = root / "benchmark-results-v0.4.1.json"
    v05_file = root / "benchmark-results-v0.5.0.json"

    assert v03_file.is_file(), "Historical v0.3.0 benchmark file must exist"
    assert v04_file.is_file(), "Historical v0.4.1 benchmark file must exist"
    assert v05_file.is_file(), "v0.5.0 benchmark file must exist"

    v03_data = json.loads(v03_file.read_text(encoding="utf-8"))
    v04_data = json.loads(v04_file.read_text(encoding="utf-8"))
    v05_data = json.loads(v05_file.read_text(encoding="utf-8"))

    assert v03_data["benchmark"] == "naviqra-platform-primitives" and v03_data["version"] == "0.3.0"
    assert "metrics" in v04_data and "iterations" in v04_data
    assert v05_data["benchmark"] == "naviqra-v0.5.0-local-microbenchmark"
    assert "metrics" in v05_data and v05_data["iterations"] == 1000

