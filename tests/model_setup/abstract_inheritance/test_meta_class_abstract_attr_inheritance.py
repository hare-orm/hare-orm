"""Repro/regression test for a hare/models/model_meta.py finding: `attrs.get("Meta", ...)` only
ever looked at the CURRENT class's own namespace, so a concrete model didn't inherit ANY Meta
attribute an abstract ancestor declared unless it explicitly wrote
`class Meta(Parent.Meta): pass` - the only existing workaround, at
hare/contrib/versioning/versioned_model.py."""

import os

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.models import Model
from hare.query.expressions import F
from tests.model_setup.abstract_inheritance.tuple_indexes import (
    TupleIndexDiamond,
    TupleIndexPlain,
    TupleIndexRenamedColumn,
)
from tests.utils.model_bindings import ModelBindings


@pytest.mark.database_independent
def test_abstract_meta_attr_inherited_through_multi_level_chain():
    declaration_graph = ModelBindingGraph()

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

    assert declaration_graph.include(Child).db_table == "from_grandparent"


@pytest.mark.database_independent
def test_concrete_ancestors_meta_table_is_not_inherited():
    """The merge loop above is deliberately scoped to ABSTRACT ancestors only - subclassing a
    CONCRETE (non-abstract) model with no Meta of its own used to still walk its full MRO and
    merge in the concrete ancestor's own Meta.table/schema, silently mapping two unrelated
    models onto the identical physical table."""

    declaration_graph = ModelBindingGraph()

    class ConcreteBase(Model):
        class Meta:
            table = "from_concrete_base"

        shared = fields.CharField(max_length=10)

    class ConcreteChild(ConcreteBase):
        extra = fields.CharField(max_length=10, null=True)

    assert declaration_graph.include(ConcreteChild).db_table != "from_concrete_base"
    assert declaration_graph.include(ConcreteBase).db_table == "from_concrete_base"


@pytest.mark.database_independent
def test_a_concrete_model_with_the_meta_of_an_abstract_one_hands_none_of_it_down():
    """A concrete model's ``class Meta(Base.Meta)`` inherited ``abstract = True`` from the abstract
    model's - a model subclassing the concrete one took its constraints, indexes and ordering, two
    tables then declaring constraints of one name."""

    declaration_graph = ModelBindingGraph()

    class AbstractBase(Model):
        level = fields.IntField(default=0)

        class Meta:
            abstract = True
            ordering = ["-level"]

    class ConcreteParent(AbstractBase):
        code = fields.CharField(max_length=10)

        class Meta(AbstractBase.Meta):
            indexes = [Index(fields=["level"], name="concrete_parent_level")]
            constraints = [UniqueConstraint(fields=["code"], name="concrete_parent_code")]

    class PlainParent(AbstractBase):
        class Meta(AbstractBase.Meta):
            pass

    class ConcreteChild(ConcreteParent):
        extra = fields.IntField(null=True)

    assert not declaration_graph.include(ConcreteParent).abstract
    assert not declaration_graph.include(PlainParent).abstract
    assert [index.name for index in declaration_graph.include(ConcreteParent).indexes] == ["concrete_parent_level"]
    assert list(declaration_graph.include(ConcreteChild).indexes) == []
    assert list(declaration_graph.include(ConcreteChild).constraints) == []
    # The abstract model's ordering still reaches it.
    assert [field for field, __ in declaration_graph.include(ConcreteChild).ordering] == ["level"]


@pytest.mark.database_independent
def test_abstract_meta_indexes_not_aliased_across_sibling_concrete_models():
    """Sibling models keep separate index declarations and use their own physical columns."""

    declaration_graph = ModelBindingGraph()

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

    assert declaration_graph.include(SiblingOne).indexes[0] is not declaration_graph.include(SiblingTwo).indexes[0]


@pytest.mark.database_independent
def test_abstract_meta_tuple_indexes_and_constraints_not_aliased():
    apps = ModelBindings.prepare(TupleIndexPlain, TupleIndexRenamedColumn)
    """An abstract base declaring Meta.indexes as a tuple (not a list) got no copy, so sibling
    concrete models shared the same Index objects."""
    assert apps.get_binding(TupleIndexPlain).indexes[0] is not apps.get_binding(TupleIndexRenamedColumn).indexes[0]
    assert (
        apps.get_binding(TupleIndexPlain).constraints[0]
        is not apps.get_binding(TupleIndexRenamedColumn).constraints[0]
    )
    assert apps.get_binding(TupleIndexPlain).constraints == apps.get_binding(TupleIndexRenamedColumn).constraints


@pytest.mark.database_independent
def test_abstract_meta_indexes_reached_through_diamond_kept_once():
    apps = ModelBindings.prepare(TupleIndexDiamond)
    assert len(apps.get_binding(TupleIndexDiamond).indexes) == 1
    assert len(apps.get_binding(TupleIndexDiamond).constraints) == 1


@pytest.mark.asyncio
async def test_abstract_meta_tuple_index_expression_uses_each_model_column():
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        ["tests.model_setup.abstract_inheritance.tuple_indexes"], db_url=db_url, app_label="models"
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
