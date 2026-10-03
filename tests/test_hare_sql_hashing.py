"""Regression coverage for hare.sql's cheap Table.__hash__ and memoized
Term.__hash__ (a hot-path optimization) - both must keep matching their
existing __eq__/content semantics, not just be fast."""

from hare.sql import Field, Table
from hare.sql.queries import Schema


def test_table_hash_matches_equal_tables():
    a = Table("widget")
    b = Table("widget")
    assert a == b
    assert hash(a) == hash(b)


def test_table_hash_differs_for_different_names():
    assert hash(Table("widget")) != hash(Table("gadget"))


def test_table_hash_accounts_for_schema():
    plain = Table("widget")
    schema_a = Table("widget", schema="tenant_a")
    schema_b = Table("widget", schema="tenant_b")
    assert hash(plain) != hash(schema_a)
    assert hash(schema_a) != hash(schema_b)
    assert Table("widget", schema="tenant_a") == schema_a
    assert hash(Table("widget", schema="tenant_a")) == hash(schema_a)


def test_table_hash_accounts_for_nested_schema():
    nested_a = Table("widget", schema=Schema("inner", parent=Schema("outer")))
    nested_b = Table("widget", schema=Schema("inner", parent=Schema("outer")))
    nested_c = Table("widget", schema=Schema("inner", parent=Schema("other")))
    assert nested_a == nested_b
    assert hash(nested_a) == hash(nested_b)
    assert hash(nested_a) != hash(nested_c)


def test_table_hash_accounts_for_alias():
    plain = Table("widget")
    aliased = Table("widget").as_("w")
    assert hash(plain) != hash(aliased)


def test_table_set_dedups_by_identity_not_object():
    tables = {Table("widget"), Table("widget"), Table("gadget")}
    assert len(tables) == 2


def test_term_hash_is_stable_across_calls():
    """Memoized hash must return the same value on repeated access."""
    field = Field("name", table=Table("widget"))
    assert hash(field) == hash(field)


def test_term_hash_matches_between_equivalent_fields():
    """Two distinct Field objects referring to the same column render identical SQL
    and must still collapse into one set() entry, exactly as before memoization."""
    a = Field("name", table=Table("widget"))
    b = Field("name", table=Table("widget"))
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_term_hash_differs_for_different_fields():
    a = Field("name", table=Table("widget"))
    b = Field("price", table=Table("widget"))
    assert hash(a) != hash(b)
