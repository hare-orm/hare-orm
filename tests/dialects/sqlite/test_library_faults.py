"""Faults of particular SQLite libraries hare leaves to the library - each test skips on the versions
having the fault: SQLite 3.38.0-3.41.0 build automatic indexes that ignore a comparison's collating
sequence, SQLite 3.38.0-3.38.5 report a foreign key broken by a statement with RETURNING as a plain
error."""

from __future__ import annotations

import sqlite3

import pytest

from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.exceptions import IntegrityError

pytestmark = pytest.mark.sqlite_only


@pytest.mark.skipif(
    (3, 38, 0) <= sqlite3.sqlite_version_info < (3, 41, 1),
    reason="SQLite 3.38.0-3.41.0 build automatic indexes that ignore a comparison's collating sequence",
)
@pytest.mark.asyncio
async def test_a_collated_comparison_over_two_joins_of_one_table_finds_its_rows():
    client = AiosqliteClient(file_path=":memory:", connection_alias="collated_joins")
    try:
        await client.execute_script(
            "CREATE TABLE shop (id INTEGER PRIMARY KEY);"
            "CREATE TABLE sale (id INTEGER PRIMARY KEY, shop_id INT REFERENCES shop (id), total TEXT);"
            "CREATE INDEX sale_shop ON sale (shop_id);"
            "INSERT INTO shop VALUES (1), (2);"
            "INSERT INTO sale VALUES (1, 1, '100.00'), (2, 1, '50.00'), (3, 2, '30.00');"
        )
        rows = await client.execute_dicts(
            "SELECT shop.id FROM shop LEFT JOIN sale first_sale ON shop.id = first_sale.shop_id "
            "LEFT JOIN sale second_sale ON shop.id = second_sale.shop_id "
            "WHERE (first_sale.total COLLATE hare_decimal) = ? AND (second_sale.total COLLATE hare_decimal) = ?",
            ["100", "50"],
        )
    finally:
        await client.close()
    assert rows == [{"id": 1}]


@pytest.mark.skipif(
    (3, 38, 0) <= sqlite3.sqlite_version_info < (3, 39, 0),
    reason="SQLite 3.38.0-3.38.5 report a foreign key broken by a statement with RETURNING as a plain error",
)
@pytest.mark.asyncio
async def test_a_foreign_key_broken_with_returning_is_an_integrity_error():
    client = AiosqliteClient(file_path=":memory:", connection_alias="returning_foreign_key")
    try:
        await client.execute_script(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INT REFERENCES parent (id));"
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await client.execute_dicts("INSERT INTO child (parent_id) VALUES (99) RETURNING id")
        await client.execute_script("INSERT INTO child (id, parent_id) VALUES (1, NULL)")
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await client.execute_dicts("UPDATE child SET parent_id = 99 WHERE id = 1 RETURNING id")
    finally:
        await client.close()
