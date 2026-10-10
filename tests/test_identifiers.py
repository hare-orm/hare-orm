"""Generated names stay within the identifier length limit of every registered dialect."""

import pytest

from hare.dialects.base.features import Features
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ConfigurationError
from hare.sql.builder.tables import Table
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT
from hare.sql.identifiers import Identifiers


def test_limit_is_the_shortest_among_builtin_dialects():
    assert POSTGRESQL_DIALECT.features.max_identifier_length == 63
    assert SQLITE_DIALECT.features.max_identifier_length is None
    assert SqlDialect().features.max_identifier_length is None
    assert IDENTIFIER_LENGTH_LIMIT == 63


def test_a_dialect_keeping_shorter_names_is_refused():
    """hare generates names up to IDENTIFIER_LENGTH_LIMIT bytes - a dialect truncating them
    earlier would make two of them one identifier."""

    class ShortNamesDialect(SqlDialect):
        name = "short_names"
        features = Features(max_identifier_length=30)

    with pytest.raises(ConfigurationError, match="within 30 bytes"):
        DialectRegistry.register_dialect(ShortNamesDialect())
    assert "short_names" not in DialectRegistry.dialects_by_name


def test_multibyte_join_alias_is_shortened_by_bytes():
    """A join alias of multi-byte characters used to be cut by characters: 52 Cyrillic letters are
    104 bytes, so the "shortened" alias was still over the limit and the database truncated it
    itself - two long aliases sharing a prefix then collided."""
    shared_prefix = "связанная_таблица_" * 4
    first_alias = Table("item").as_(f"{shared_prefix}первая").alias
    second_alias = Table("item").as_(f"{shared_prefix}вторая").alias
    assert first_alias != second_alias
    assert len(first_alias.encode()) <= 63
    assert len(second_alias.encode()) <= 63


def test_short_join_alias_is_kept():
    assert Table("item").as_("item__events").alias == "item__events"


def test_shorten_fills_the_limit_and_is_deterministic():
    name = "x" * 100
    shortened = Identifiers.shorten(name, 30)
    assert len(shortened.encode()) == 30
    assert shortened == Identifiers.shorten(name, 30)
    assert Identifiers.shorten(name, None) == name
    assert Identifiers.shorten("short", 30) == "short"


def test_generated_names_follow_the_registry_limit():
    name = "a" * 80
    assert Identifiers.get_within_limit(name) == Identifiers.shorten(name, 63)
