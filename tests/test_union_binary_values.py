"""A union's count()/explain() bind their filter values as parameters, and a bytes value rendered
inline (a union's own rows) becomes the dialect's hex binary literal, never Python's repr."""

import pytest

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.sql.context import SqlContext
from hare.sql.terms import ValueWrapper
from tests.testmodels import BinaryFields

QUOTE_BREAKING_BYTES = b"it's\x00'); DROP TABLE x; --"


@pytest.mark.asyncio
async def test_union_count_and_explain_with_a_binary_filter(db):
    await BinaryFields.objects.create(id=1, binary=QUOTE_BREAKING_BYTES)
    await BinaryFields.objects.create(id=2, binary=b"other")
    union = BinaryFields.objects.filter(binary=QUOTE_BREAKING_BYTES).union(BinaryFields.objects.filter(id=2))
    assert await union.count() == 2
    assert sorted(instance.id for instance in await union) == [1, 2]
    assert len(str(await union.explain())) > 20
    assert len(str(await BinaryFields.objects.filter(binary=QUOTE_BREAKING_BYTES).explain())) > 20


@pytest.mark.parametrize(
    ("dialect", "expected_sql"),
    [
        (SQLITE_DIALECT, "X'00ff27'"),
        (POSTGRESQL_DIALECT, "'\\x00ff27'::bytea"),
    ],
)
def test_inline_bytes_render_as_a_hex_literal(dialect, expected_sql):
    assert (
        ValueWrapper(b"\x00\xff'").get_sql(
            SqlContext(quote_char='"', secondary_quote_char="'", alias_quote_char='"', dialect=dialect)
        )
        == expected_sql
    )


def test_inline_value_of_an_unknown_type_is_rejected():
    with pytest.raises(TypeError, match="Can't render a object value as an SQL literal"):
        ValueWrapper(object()).get_sql(
            SqlContext(quote_char='"', secondary_quote_char="'", alias_quote_char='"', dialect=SQLITE_DIALECT)
        )
