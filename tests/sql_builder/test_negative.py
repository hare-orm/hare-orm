import sqlite3

import pytest

from hare.dialects.sqlite.query import SqliteQuery
from hare.sql import Field, Table

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (-(Field("a") + Field("b")), -7),
        (-(Field("a") - Field("b")), 3),
        (-(-Field("a")), 2),
        (-Field("a"), -2),
        (-(Field("a").as_("child") + Field("b")), -7),
    ],
)
def test_unary_minus_preserves_the_entire_operand_and_selected_alias(expression, expected):
    expression = expression.as_("answer")
    query = SqliteQuery.from_(Table("sample")).select(expression)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE sample(a INTEGER, b INTEGER)")
        connection.execute("INSERT INTO sample VALUES(2, 5)")
        result = connection.execute(query.get_sql())
        assert result.fetchone() == (expected,)
        assert result.description[0][0] == "answer"
    assert "answer" not in expression.get_sql(SqliteQuery.SQL_CONTEXT.copy(with_alias=False))


def test_unary_minus_does_not_render_an_operands_alias():
    expression = -Field("a").as_("child")
    assert "child" not in expression.get_sql(SqliteQuery.SQL_CONTEXT.copy(with_alias=True))
