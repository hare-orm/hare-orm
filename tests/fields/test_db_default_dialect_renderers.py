"""db_default SQL comes from the dialect the DDL runs on - its renderers, or the standard SQL."""

import pytest

from hare.dialects.base.renderers import TermRenderers
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import UnSupportedError
from hare.fields.db_defaults import Now, RandomHex, SqlDefault
from hare.fields.enums import NowValueType


class UpperCaseDefaultsDialect(SqlDialect):
    """A dialect of its own writing every SqlDefault upper-cased."""

    name = "upper_case_defaults"

    def build_renderers(self) -> TermRenderers:
        renderers = TermRenderers()
        renderers.register(SqlDefault, lambda default, ctx: default.sql.upper())
        return renderers


def test_plain_sql_uses_the_standard_sql():
    assert SqlDefault("40 + 2").get_sql() == "40 + 2"
    assert Now().get_sql() == "CURRENT_TIMESTAMP"
    assert Now(NowValueType.DATE).get_sql() == "CURRENT_DATE"
    assert Now(NowValueType.TIME).get_sql() == "CURRENT_TIME"


def test_random_hex_without_a_renderer_raises():
    """RandomHex used to fall back to SQLite's expression on any other dialect."""
    with pytest.raises(UnSupportedError, match="RandomHex"):
        RandomHex().get_sql()


def test_builtin_dialects_render_their_own_sql():
    assert RandomHex().get_sql(SQLITE_DIALECT) == "(lower(hex(randomblob(16))))"
    assert RandomHex().get_sql(POSTGRESQL_DIALECT) == "md5(random()::text)"
    assert SqlDefault("40 + 2").get_sql(SQLITE_DIALECT) == "(40 + 2)"
    assert SqlDefault("40 + 2").get_sql(POSTGRESQL_DIALECT) == "40 + 2"
    assert Now().get_sql(POSTGRESQL_DIALECT) == "STATEMENT_TIMESTAMP()"


def test_a_dialect_of_its_own_renders_through_its_renderers():
    dialect = UpperCaseDefaultsDialect()
    assert SqlDefault("lower('x')").get_sql(dialect) == "LOWER('X')"
    # Now and RandomHex are SqlDefault subclasses - the nearest registered class decides.
    assert Now().get_sql(dialect) == "CURRENT_TIMESTAMP"
