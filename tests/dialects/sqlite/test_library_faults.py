"""Faults of particular SQLite libraries hare works around: SQLite 3.38.0-3.41.0 builds automatic
indexes that ignore a comparison's collating sequence (a decimal or time comparison over a joined
table found no rows) - their connections build none; SQLite 3.38.x reports a foreign key broken by a
statement with RETURNING as a plain error - raised as the IntegrityError it is."""

from __future__ import annotations

import sqlite3

import pytest

from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.exceptions import ConfigurationError, IntegrityError, OperationalError


def test_the_driver_knows_the_faults_of_its_library():
    version = sqlite3.sqlite_version_info
    assert AiosqliteClient.has_automatic_index_collation_fault is ((3, 38, 0) <= version < (3, 41, 1))
    assert AiosqliteClient.has_returning_foreign_key_error_fault is ((3, 38, 0) <= version < (3, 39, 0))


def test_a_library_with_the_automatic_index_fault_builds_no_automatic_indexes(monkeypatch):
    monkeypatch.setattr(AiosqliteClient, "has_automatic_index_collation_fault", True)
    assert AiosqliteClient(file_path=":memory:", connection_alias="faulty").pragmas["automatic_index"] == "OFF"
    assert (
        AiosqliteClient(file_path=":memory:", connection_alias="faulty", automatic_index=False).pragmas[
            "automatic_index"
        ]
        == "OFF"
    )
    with pytest.raises(ConfigurationError, match="automatic_index=ON on SQLite .* fixed in SQLite 3.41.1"):
        AiosqliteClient(file_path=":memory:", connection_alias="faulty", automatic_index=True)


def test_a_library_without_the_fault_keeps_automatic_indexes(monkeypatch):
    monkeypatch.setattr(AiosqliteClient, "has_automatic_index_collation_fault", False)
    assert "automatic_index" not in AiosqliteClient(file_path=":memory:", connection_alias="sound").pragmas
    assert (
        AiosqliteClient(file_path=":memory:", connection_alias="sound", automatic_index=True).pragmas[
            "automatic_index"
        ]
        == "ON"
    )


@pytest.mark.asyncio
async def test_the_connection_follows_the_library():
    client = AiosqliteClient(file_path=":memory:", connection_alias="automatic_index")
    try:
        rows = await client.execute_dicts("PRAGMA automatic_index")
    finally:
        await client.close()
    assert rows == [{"automatic_index": 0 if AiosqliteClient.has_automatic_index_collation_fault else 1}]


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


@pytest.mark.parametrize("has_fault", [True, False])
@pytest.mark.asyncio
async def test_only_the_faulty_library_turns_the_plain_error_into_an_integrity_error(monkeypatch, has_fault):
    monkeypatch.setattr(AiosqliteClient, "has_returning_foreign_key_error_fault", has_fault)
    client = AiosqliteClient(file_path=":memory:", connection_alias="returning_foreign_key_message")
    try:
        await client.create_connection(with_db=True)

        async def raise_plain_error(script):
            raise sqlite3.OperationalError("FOREIGN KEY constraint failed")

        monkeypatch.setattr(client._connection, "executescript", raise_plain_error)
        with pytest.raises(IntegrityError if has_fault else OperationalError) as raised:
            await client.execute_script("SELECT 1")
        assert isinstance(raised.value, IntegrityError) is has_fault
    finally:
        await client.close()
