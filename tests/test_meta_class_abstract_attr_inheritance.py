"""Repro/regression test for a hare/models/model_meta.py finding: `attrs.get("Meta", ...)` only
ever looked at the CURRENT class's own namespace, so a concrete model didn't inherit ANY Meta
attribute an abstract ancestor declared unless it explicitly wrote
`class Meta(Parent.Meta): pass` - the only existing workaround, at
hare/contrib/versioning/versioned_model.py."""

import os

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.models import Model
from hare.query.expressions import F
from tests.model_setup.model_abstract_tuple_indexes import (
    TupleIndexDiamond,
    TupleIndexPlain,
    TupleIndexRenamedColumn,
)


def test_abstract_meta_attr_inherited_without_explicit_meta_class():
    class AbstractBase(Model):
        class Meta:
            abstract = True
            table = "from_base"

        shared = fields.CharField(max_length=10)

    class Concrete(AbstractBase):
        pass

    assert Concrete._meta.db_table == "from_base"


def test_concrete_meta_attr_overrides_inherited_value():
    class AbstractBase(Model):
        class Meta:
            abstract = True
            table = "from_base"

        shared = fields.CharField(max_length=10)

    class Concrete(AbstractBase):
        class Meta:
            table = "from_concrete"

    assert Concrete._meta.db_table == "from_concrete"


def test_abstract_meta_attr_inherited_through_multi_level_chain():
    class GrandParent(Model):
        class Meta:
            abstract = True
            table = "from_grandparent"

        shared = fields.CharField(max_length=10)

    class Parent(GrandParent):
        class Meta:
            abstract = True

    class Child(Parent):
        pass

    assert Child._meta.db_table == "from_grandparent"


def test_abstract_flag_itself_is_never_inherited():
    class AbstractBase(Model):
        class Meta:
            abstract = True
            table = "from_base"

        shared = fields.CharField(max_length=10)

    class Concrete(AbstractBase):
        pass

    assert Concrete._meta.abstract is False


def test_multiple_meta_attrs_all_inherited_together():
    class AbstractBase(Model):
        class Meta:
            abstract = True
            table = "from_base"
            constraints = (UniqueConstraint(fields=("shared",)),)
            ordering = ("-shared",)

        shared = fields.CharField(max_length=10)

    class Concrete(AbstractBase):
        pass

    assert Concrete._meta.db_table == "from_base"
    assert [tuple(constraint.fields) for constraint in Concrete._meta.constraints] == [("shared",)]
    assert [field for field, _ in Concrete._meta.ordering] == ["shared"]


def test_concrete_ancestors_meta_table_is_not_inherited():
    """The merge loop above is deliberately scoped to ABSTRACT ancestors only - subclassing a
    CONCRETE (non-abstract) model with no Meta of its own used to still walk its full MRO and
    merge in the concrete ancestor's own Meta.table/schema, silently mapping two unrelated
    models onto the identical physical table."""

    class ConcreteBase(Model):
        class Meta:
            table = "from_concrete_base"

        shared = fields.CharField(max_length=10)

    class ConcreteChild(ConcreteBase):
        extra = fields.CharField(max_length=10, null=True)

    assert ConcreteChild._meta.db_table != "from_concrete_base"
    assert ConcreteBase._meta.db_table == "from_concrete_base"


def test_abstract_meta_indexes_not_aliased_across_sibling_concrete_models():
    """Every attribute value inherited from an abstract ancestor's Meta used to be copied by
    bare REFERENCE, not value - harmless for immutable content, but an Index(...) entry in
    Meta.indexes is a plain mutable object whose own get_expressions() permanently memoizes its
    resolved SQL terms against whichever model calls it FIRST (Index._expressions_compiled).
    Two sibling concrete models sharing the identical Index object out of an inherited abstract
    Meta.indexes used to mean the SECOND sibling's own DDL silently kept referencing the FIRST
    sibling's own table/columns forever - confirmed live to generate a UNIQUE index referencing
    a column that doesn't even exist on the second sibling's real table."""

    class AbstractBase(Model):
        id = fields.IntField(primary_key=True)
        group = fields.CharField(max_length=16)

        class Meta:
            abstract = True
            indexes = [Index(F("group"), unique=True)]

    class SiblingOne(AbstractBase):
        class Meta:
            table = "sibling_one"

    class SiblingTwo(AbstractBase):
        class Meta:
            table = "sibling_two"

    assert SiblingOne._meta.indexes[0] is not SiblingTwo._meta.indexes[0]


def test_abstract_meta_tuple_indexes_and_constraints_not_aliased():
    """An abstract base declaring Meta.indexes as a tuple (not a list) got no copy, so sibling
    concrete models shared the same Index objects."""
    assert TupleIndexPlain._meta.indexes[0] is not TupleIndexRenamedColumn._meta.indexes[0]
    assert TupleIndexPlain._meta.constraints[0] is not TupleIndexRenamedColumn._meta.constraints[0]
    assert TupleIndexPlain._meta.constraints == TupleIndexRenamedColumn._meta.constraints


def test_abstract_meta_indexes_reached_through_diamond_kept_once():
    assert len(TupleIndexDiamond._meta.indexes) == 1
    assert len(TupleIndexDiamond._meta.constraints) == 1


@pytest.mark.asyncio
async def test_abstract_meta_tuple_index_expression_uses_each_model_column():
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        ["tests.model_setup.model_abstract_tuple_indexes"], db_url=db_url, app_label="models"
    ) as ctx:
        index_lines = [
            line
            for line in ctx.get_connection().get_schema_sql(safe=False).splitlines()
            if line.strip().upper().startswith("CREATE INDEX")
        ]
        quote = ctx.get_connection().dialect.literals.quote_identifier
        plain_line = next(line for line in index_lines if quote("tuple_index_plain") in line)
        renamed_line = next(line for line in index_lines if quote("tuple_index_renamed_column") in line)
        assert quote("title") in plain_line
        assert quote("title_renamed") in renamed_line
        await TupleIndexRenamedColumn.objects.create(title="x")
        assert await TupleIndexRenamedColumn.objects.filter(title="x").exists()
