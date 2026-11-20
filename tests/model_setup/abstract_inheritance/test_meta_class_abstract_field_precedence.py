"""Repro/regression test for a hare/models/model_meta.py finding."""

import pytest

from hare import fields
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.models import Model

pytestmark = pytest.mark.database_independent


def test_abstract_base_field_precedence_left_to_right():
    """_search_for_field_attributes's abstract-base branch overwrote attrs[key]
    unconditionally, unlike the mixin branch right below it (which correctly guards with
    `key not in attrs`) - for multiple inheritance from two abstract Model ancestors declaring a
    same-named field, the RIGHTMOST ancestor won, contradicting both the method's own docstring
    ("derived classes have higher precedence... Multiple Inheritance is supported from left to
    right") and the mixin branch's actual behavior for the exact same scenario."""

    declaration_graph = ModelBindingGraph()

    class AbstractA(Model):
        class Meta:
            abstract = True

        shared = fields.CharField(max_length=10)

    class AbstractB(Model):
        class Meta:
            abstract = True

        shared = fields.CharField(max_length=99)

    class Concrete(AbstractA, AbstractB):
        class Meta:
            abstract = True

    assert declaration_graph.include(Concrete).fields_map["shared"].max_length == 10


def test_field_override_at_intermediate_level_wins_over_a_more_distant_ancestor():
    """A concrete descendant inherits the closest field override, even when the original declaration is also an
    ancestor.
    """

    declaration_graph = ModelBindingGraph()

    class GrandParent(Model):
        class Meta:
            abstract = True

        shared = fields.IntField(default=1)

    class Parent(GrandParent):
        class Meta:
            abstract = True

        shared = fields.IntField(default=2)

    class Child(Parent):
        pass

    assert declaration_graph.include(Child).fields_map["shared"].default == 2


def test_diamond_inheritance_field_precedence_follows_real_mro_not_bases_order():
    """Mixin1 is listed FIRST in DiamondConcrete's bases but passively inherits `shared` from the
    shared abstract ancestor AbstractA without overriding it; Mixin2 (listed second) genuinely
    overrides it. DiamondConcrete's real Python MRO is
    [DiamondConcrete, Mixin1, Mixin2, AbstractA, ...] - Mixin2's own override is the nearer
    declaration and must win, regardless of which base is listed first. ModelMeta used to commit
    each base's ENTIRE already-resolved field set (including whatever it merely passed through
    from a shared ancestor) before ever looking at the next base, so Mixin1's passthrough of
    AbstractA's original value claimed the slot before Mixin2's own override got a chance to."""

    declaration_graph = ModelBindingGraph()

    class AbstractA(Model):
        shared = fields.CharField(max_length=10)

        class Meta:
            abstract = True

    class Mixin1(AbstractA):
        class Meta:
            abstract = True

        # Does not override `shared` - passively inherits AbstractA's.

    class Mixin2(AbstractA):
        shared = fields.CharField(max_length=99)

        class Meta:
            abstract = True

    class DiamondConcrete(Mixin1, Mixin2):
        pass

    assert declaration_graph.include(DiamondConcrete).fields_map["shared"].max_length == 99


def test_diamond_inheritance_own_override_wins_even_when_ancestor_also_declares_it():
    """A branch that redeclares a field contributes it even when another branch inherits it unchanged."""

    declaration_graph = ModelBindingGraph()

    class Base(Model):
        val = fields.IntField(default=1)

        class Meta:
            abstract = True

    class Overrider(Base):
        val = fields.IntField(default=2)

        class Meta:
            abstract = True

    class Passthrough(Base):
        class Meta:
            abstract = True

    class DiamondOverride(Overrider, Passthrough):
        pass

    assert declaration_graph.include(DiamondOverride).fields_map["val"].default == 2
