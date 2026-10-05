"""ClickHouse's client writes every parameter into its statement as a literal - these run without a
server, on any test database."""

import datetime
import decimal
import uuid

import pytest

pytest.importorskip("clickhouse_connect")

from hare.ddl.raw_sql_term import RawSQLTerm  # noqa: E402
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator  # noqa: E402
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions  # noqa: E402
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (  # noqa: E402
    ClickhouseConnectClient,
)
from hare.exceptions import ConfigurationError  # noqa: E402

literals = CLICKHOUSE_DIALECT.literals


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (None, "NULL"),
        (True, "true"),
        (42, "42"),
        (1.5, "1.5"),
        (float("nan"), "nan"),
        (decimal.Decimal("12.50"), "toDecimal128('12.50', 2)"),
        (decimal.Decimal("7"), "toDecimal128('7', 0)"),
        ("it's a \\ path", "'it\\'s a \\\\ path'"),
        (b"\x00\xff", "unhex('00ff')"),
        (datetime.date(2024, 1, 2), "toDate32('2024-01-02')"),
        (
            datetime.datetime(2024, 1, 2, 5, 4, 5, 6, tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
            "toDateTime64('2024-01-02 03:04:05.000006', 6, 'UTC')",
        ),
        (uuid.UUID("6f1e5a0e-0000-4000-8000-000000000001"), "toUUID('6f1e5a0e-0000-4000-8000-000000000001')"),
        ([1, "a"], "[1,'a']"),
    ],
)
def test_a_value_is_written_as_its_literal(value, literal):
    assert literals.get_literal_sql(value) == literal


def make_client():
    return ClickhouseConnectClient(connection_alias="clickhouse", host="example", database="analytics")


def test_placeholders_are_replaced_outside_quotes():
    client = make_client()
    query = "SELECT '$1', \"$2\", `$1` FROM t WHERE a = $1 AND b = $2 AND c = $10"
    values = [1, "x", *range(3, 10), 10]
    assert client.get_inlined_query(query, values) == (
        "SELECT '$1', \"$2\", `$1` FROM t WHERE a = 1 AND b = 'x' AND c = 10"
    )
    assert client.get_inlined_query("SELECT 'a\\'$1' WHERE x = $1", [5]) == "SELECT 'a\\'$1' WHERE x = 5"


def test_a_script_is_split_outside_quotes():
    client = make_client()
    assert client.split_script("SELECT ';'; SELECT 2;\n\n") == ["SELECT ';'", "SELECT 2"]


@pytest.mark.parametrize(
    ("query", "inlined"),
    [
        # A doubled quote stands for itself - the string goes on.
        ("SELECT 'it''s $1' WHERE a = $1", "SELECT 'it''s $1' WHERE a = 7"),
        ('SELECT "a""$1" FROM t WHERE a = $1', 'SELECT "a""$1" FROM t WHERE a = 7'),
        ("SELECT `a``$1` FROM t WHERE a = $1", "SELECT `a``$1` FROM t WHERE a = 7"),
        # A backslash escapes the quote after it.
        ("SELECT 'a\\'b $1' WHERE a = $1", "SELECT 'a\\'b $1' WHERE a = 7"),
        # A $ without a number stays.
        ("SELECT $a, $ WHERE a = $1", "SELECT $a, $ WHERE a = 7"),
        # An unclosed string runs to the end.
        ("SELECT $1, 'open $1", "SELECT 7, 'open $1"),
    ],
)
def test_quoted_parts_keep_their_placeholders(query, inlined):
    assert make_client().get_inlined_query(query, [7]) == inlined


def test_a_negative_value_after_a_minus_sign_starts_no_comment():
    """`"n"-$1` with -2 was sent as `"n"--2`: the rest of the statement became a comment."""
    client = make_client()
    assert client.get_inlined_query('SELECT "n"-$1+$2 FROM t', [-2, 3]) == 'SELECT "n"- -2+3 FROM t'
    assert client.get_inlined_query('SELECT "n"-$1 FROM t', [-1.5]) == 'SELECT "n"- -1.5 FROM t'


def test_braces_of_the_statement_stay_as_they_are():
    client = make_client()
    assert client.get_inlined_query("SELECT {x:UInt8}, '{$1}' WHERE a = $1 {}", [4]) == (
        "SELECT {x:UInt8}, '{$1}' WHERE a = 4 {}"
    )
    # The layout of a statement is kept - the same text with other values.
    assert client.get_inlined_query("SELECT {x:UInt8}, '{$1}' WHERE a = $1 {}", ["{}"]) == (
        "SELECT {x:UInt8}, '{$1}' WHERE a = '{}' {}"
    )


def test_a_script_keeps_the_semicolons_of_doubled_and_escaped_quotes():
    client = make_client()
    script = "SELECT 'a'';'; SELECT \"b\\\";\"; SELECT `c;`; SELECT 'open;"
    assert client.split_script(script) == ["SELECT 'a'';'", 'SELECT "b\\";"', "SELECT `c;`", "SELECT 'open;"]


def test_the_error_code_is_read_from_the_message():
    assert ClickhouseConnectClient.get_error_code(Exception("Code: 469. DB::Exception: violated")) == 469
    assert ClickhouseConnectClient.get_error_code(Exception("no code here")) is None


def test_a_db_url_names_the_database_and_settings():
    config = DbUrlConfigGenerator.expand(
        "clickhouse+clickhouse-connect://user:secret@db.local:9000/analytics?secure=true&connect_timeout=5"
    )
    assert config["engine"] == "clickhouse+clickhouse-connect"
    assert config["credentials"] == {
        "host": "db.local",
        "port": 9000,
        "user": "user",
        "password": "secret",
        "database": "analytics",
        "secure": True,
        "connect_timeout": 5.0,
    }
    client = ClickhouseConnectClient(connection_alias="clickhouse", **config["credentials"])
    assert client.options["secure"] is True
    assert client.options["connect_timeout"] == 5
    with pytest.raises(ConfigurationError, match="connect_timeout"):
        ClickhouseConnectClient(connection_alias="clickhouse", host="h", connect_timeout=0)
    with pytest.raises(ConfigurationError):
        DbUrlConfigGenerator.expand("clickhouse+clickhouse-connect://h/db?no_such_option=1")


def test_table_options_are_checked():
    with pytest.raises(ConfigurationError, match="engine"):
        ClickhouseTableOptions(engine="")
    with pytest.raises(ConfigurationError, match="order_by"):
        ClickhouseTableOptions(order_by=["id"])  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="settings"):
        ClickhouseTableOptions(settings=(("index_granularity", True),))
    with pytest.raises(ConfigurationError, match="partition_by takes RawSQLTerm"):
        ClickhouseTableOptions(partition_by="toYYYYMM(viewed_at)")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="ttl takes RawSQLTerm"):
        ClickhouseTableOptions(ttl=RawSQLTerm(" "))


@pytest.mark.parametrize(
    ("sql", "returns_rows"),
    [
        ("SELECT 1", True),
        ("  (SELECT 1)", True),
        ("/* tag */ SELECT 1", True),
        ("-- note\nWITH x AS (SELECT 1) SELECT * FROM x", True),
        ("/* a */ -- b\n /* c */ show tables", True),
        ("/* SELECT */ INSERT INTO t VALUES (1)", False),
        ("-- only a comment", False),
        ("", False),
    ],
)
def test_a_statement_returns_rows_by_its_first_word_after_comments(sql, returns_rows):
    assert ClickhouseConnectClient.returns_rows(sql) is returns_rows
