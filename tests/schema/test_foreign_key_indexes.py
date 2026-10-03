"""generate_schemas() DDL for the index a foreign key gets by default, on both dialects."""

import re
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from tests.schema.test_generate_schema import _init_for_asyncpg, _init_for_sqlite, _reset_hare, _teardown_hare

MODELS_MODULE = "tests.schema.models_foreign_key_index"


def get_plain_index_columns(sqls: list[str], table: str) -> list[tuple[str, ...]]:
    """The column lists of every plain CREATE INDEX on `table`, in DDL order."""
    index_columns: list[tuple[str, ...]] = []
    pattern = re.compile(rf'CREATE INDEX (?:IF NOT EXISTS )?"[^"]+" ON "{table}" \(([^)]*)\)(.*)')
    for statement in "; ".join(sqls).split(";"):
        match = pattern.search(statement.strip())
        if match is None:
            continue
        columns = tuple(column.strip().strip('"') for column in match.group(1).split(","))
        index_columns.append((*columns, "PARTIAL") if match.group(2).strip() else columns)
    return index_columns


@pytest_asyncio.fixture(params=["sqlite", "asyncpg"])
async def schema_sqls(request) -> AsyncGenerator[list[str]]:
    await _reset_hare()
    try:
        initialize = _init_for_sqlite if request.param == "sqlite" else _init_for_asyncpg
        yield await initialize(MODELS_MODULE)
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_foreign_key_is_indexed_unless_disabled_or_covered(schema_sqls: list[str]) -> None:
    assert get_plain_index_columns(schema_sqls, "fki_child") == [
        ("indexed_id",),
        ("pair_left", "pair_right"),
        ("second_in_index_id",),
        ("unconstrained_id",),
        ("in_meta_index_id", "code"),
        ("code", "second_in_index_id"),
        ("in_partial_index_id", "PARTIAL"),
    ]


@pytest.mark.asyncio
async def test_one_to_one_field_gets_no_index_besides_its_unique_constraint(schema_sqls: list[str]) -> None:
    child_sql = next(sql for sql in schema_sqls if 'CREATE TABLE "fki_child"' in sql)

    assert '"single_id" INT NOT NULL UNIQUE' in child_sql
    assert all("single_id" not in columns for columns in get_plain_index_columns(schema_sqls, "fki_child"))


@pytest.mark.asyncio
async def test_through_model_indexes_the_relation_its_unique_together_does_not_lead_with(
    schema_sqls: list[str],
) -> None:
    assert get_plain_index_columns(schema_sqls, "fki_membership") == [("target_left", "target_right")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("through_table", "expected_index_columns"),
    [
        ("fki_tagged_tags", [("parent_id",)]),
        ("fki_tagged_repeated", [("fki_tagged_id",), ("parent_id",)]),
        ("fki_tagged_unindexed", []),
        ("fki_tagged_pairs", [("pair_left", "pair_right")]),
    ],
)
async def test_automatic_through_table_indexes_the_key_columns_its_unique_index_does_not_lead_with(
    schema_sqls: list[str], through_table: str, expected_index_columns: list[tuple[str, ...]]
) -> None:
    assert get_plain_index_columns(schema_sqls, through_table) == expected_index_columns


@pytest.mark.asyncio
async def test_generated_index_names_are_unique(schema_sqls: list[str]) -> None:
    index_names = re.findall(r'CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?"([^"]+)"', "; ".join(schema_sqls))

    assert len(index_names) == len(set(index_names))
