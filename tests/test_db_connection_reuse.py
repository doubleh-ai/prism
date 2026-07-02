"""Thread-local connection reuse + autocommit mode.

- Same thread, multiple get_db() calls → same connection object.
- Different threads → independent connections.
- Autocommit: single-statement changes are visible without explicit commit.
"""
import sqlite3
import threading
from server import database


def test_same_thread_reuses_connection():
    """Two get_db() calls in the same thread yield the same Connection object."""
    with database.get_db() as db1:
        id1 = id(db1)
    with database.get_db() as db2:
        id2 = id(db2)
    assert id1 == id2, "Expected same connection in same thread"


def test_different_threads_have_independent_connections():
    """Different threads should not share a connection (each has its own)."""
    connections = {}

    def grab(tid):
        with database.get_db() as db:
            connections[tid] = id(db)

    t1 = threading.Thread(target=grab, args=("t1",))
    t2 = threading.Thread(target=grab, args=("t2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert connections["t1"] != connections["t2"], \
        "Different threads should have different connection objects"


def test_reset_connection_creates_new_one():
    """reset_connection() forces the next get_db() to open a fresh connection."""
    with database.get_db() as db1:
        id1 = id(db1)
    database.reset_connection()
    with database.get_db() as db2:
        id2 = id(db2)
    assert id1 != id2, "Expected fresh connection after reset_connection()"


def test_autocommit_writes_persist_immediately():
    """Under autocommit, an INSERT is visible to a subsequent SELECT
    without an explicit commit call."""
    with database.get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   ["autocommit_test", "fakehash"])
    # New context — should see the row
    with database.get_db() as db:
        row = db.execute("SELECT password_hash FROM users WHERE username=?",
                         ["autocommit_test"]).fetchone()
    assert row is not None
    assert row["password_hash"] == "fakehash"
    # Cleanup
    with database.get_db() as db:
        db.execute("DELETE FROM users WHERE username=?", ["autocommit_test"])
