"""``columnar`` - a dialect hare doesn't ship, built from its public API alone to prove a dialect
plugs in without touching hare itself.

It runs on SQLite's engine through the stdlib ``sqlite3`` driver and differs from hare's SQLite
dialect wherever that engine allows: its own name and ``columnar://`` DB_URL scheme, numbered
``?1`` placeholders, backtick-quoted identifiers, no transactions, foreign keys or unique
constraints, UUIDs stored as 16-byte BLOBs, a function rendered its own way, a QuerySet method of
its own (``sample()``) and table options of its own (``ColumnarTableOptions``).

Importing the package registers the driver; ``HARE_TEST_DB=columnar://:memory:`` runs the test
suite on it.
"""

from tests.dialects.columnar.driver import COLUMNAR_DRIVER

__all__ = ["COLUMNAR_DRIVER"]
