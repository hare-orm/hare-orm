"""Values written into SQL text as literals - column defaults, utility statements - by each dialect."""

import datetime
import sqlite3
from decimal import Decimal

import pytest

from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ValidationError


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (None, "NULL"),
        (True, "1"),
        (False, "0"),
        (7, "7"),
        (Decimal("1.50"), "1.50"),
        (0.1, "0.1"),
        ("it's a test", "'it''s a test'"),
        ("C:\\path", "'C:\\path'"),
        (datetime.date(2026, 1, 2), "'2026-01-02'"),
        (datetime.time(12, 30, 1), "'12:30:01'"),
        (datetime.timedelta(minutes=-30), "'-00:30:00'"),
        (datetime.timedelta(hours=26, microseconds=5), "'26:00:00.000005'"),
        (b"\x01\xff", "X'01ff'"),
    ],
)
def test_base_literals(value, literal):
    assert SQL_DIALECT.get_literal_sql(value) == literal


def test_a_value_of_no_known_type_is_its_repr():
    class Custom:
        def __repr__(self) -> str:
            return "custom-repr"

    assert SQL_DIALECT.get_literal_sql(Custom()) == "custom-repr"


def test_a_null_byte_is_refused():
    with pytest.raises(ValidationError, match="null byte"):
        SQL_DIALECT.get_string_literal_sql("a\x00b")


def test_postgresql_literals():
    assert POSTGRESQL_DIALECT.get_literal_sql(True) == "TRUE"
    assert POSTGRESQL_DIALECT.get_string_literal_sql("it's") == "'it''s'"
    assert POSTGRESQL_DIALECT.get_string_literal_sql("C:\\path") == "E'C:\\\\path'"
    assert POSTGRESQL_DIALECT.get_literal_sql([1, "two", False]) == "'{1,\"two\",false}'"
    assert POSTGRESQL_DIALECT.get_literal_sql([]) == "'{}'"
    assert POSTGRESQL_DIALECT.get_literal_sql(['it\'s a "test" \\ok']) == "'{\"it''s a \\\"test\\\" \\\\ok\"}'"
    aware = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    assert POSTGRESQL_DIALECT.get_literal_sql(aware) == "'2026-01-02T03:04:05+00:00'"
    assert POSTGRESQL_DIALECT.get_literal_sql(datetime.time(1, 2)) == "'01:02:00+00:00'"


def test_sqlite_literals_read_back_unchanged():
    aware = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
    assert SQLITE_DIALECT.get_literal_sql(aware) == "'2026-01-02 00:04:05+00:00'"
    connection = sqlite3.connect(":memory:")
    try:
        for value in ('it\'s a "test" \\ok', "x; DROP TABLE t; --", "юникод"):
            literal = SQLITE_DIALECT.get_literal_sql(value)
            assert connection.execute(f"SELECT {literal}").fetchone() == (value,)
    finally:
        connection.close()
