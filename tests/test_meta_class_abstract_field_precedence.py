"""Repro/regression test for a hare/models/model_meta.py finding."""

from hare import fields
from hare.models import Model


def test_abstract_base_field_precedence_left_to_right():
    """_search_for_field_attributes's abstract-base branch overwrote attrs[key]
    unconditionally, unlike the mixin branch right below it (which correctly guards with
    `key not in attrs`) - for multiple inheritance from two abstract Model ancestors declaring a
    same-named field, the RIGHTMOST ancestor won, contradicting both the method's own docstring
    ("derived classes have higher precedence... Multiple Inheritance is supported from left to
    right") and the mixin branch's actual behavior for the exact same scenario."""

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

    assert Concrete._meta.fields_map["shared"].max_length == 10


def test_field_override_at_intermediate_level_wins_over_a_more_distant_ancestor():
    """_search_for_field_attributes recursed into base.__mro__[1:] (base's own, more distant
    ancestors) BEFORE applying base's own already-resolved _meta.fields_map - so a field
    re-overridden at an intermediate level of an already-built ancestor chain lost to a MORE
    DISTANT ancestor's now-stale version of the same field, the moment that intermediate class
    was itself used as a base for yet another subclass (Parent's own correct override of `shared`
    was already fully resolved on Parent._meta.fields_map by the time Parent was built - but
    GrandParent's original value got applied to Child's attrs FIRST, via the recursive mro walk,
    before Parent's own fields_map ever got a chance to)."""

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

    assert Child._meta.fields_map["shared"].default == 2


def test_diamond_inheritance_field_precedence_follows_real_mro_not_bases_order():
    """Mixin1 is listed FIRST in DiamondConcrete's bases but passively inherits `shared` from the
    shared abstract ancestor AbstractA without overriding it; Mixin2 (listed second) genuinely
    overrides it. DiamondConcrete's real Python MRO is
    [DiamondConcrete, Mixin1, Mixin2, AbstractA, ...] - Mixin2's own override is the nearer
    declaration and must win, regardless of which base is listed first. ModelMeta used to commit
    each base's ENTIRE already-resolved field set (including whatever it merely passed through
    from a shared ancestor) before ever looking at the next base, so Mixin1's passthrough of
    AbstractA's original value claimed the slot before Mixin2's own override got a chance to."""

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

    assert DiamondConcrete._meta.fields_map["shared"].max_length == 99


def test_diamond_inheritance_own_override_wins_even_when_ancestor_also_declares_it():
    """Overrider redeclares `val` itself (rather than passively inheriting Base's), and
    Passthrough does not - both are then combined in a further diamond. Overrider's own
    redeclaration must still count as ITS OWN contribution (not merely "passed through from
    Base") when a further subclass resolves the diamond: build_meta()'s own_field_names,
    which ModelMeta._contribute_inherited_field_attributes() consults to decide "this ancestor's
    own declaration" vs "this ancestor's passthrough", used to be computed as
    `fields_map.keys() - inherited_attrs.keys()` - since `inherited_attrs` still holds `val`
    (from Base) even after Overrider's own class body shadows it in the merge, that formula
    wrongly excluded `val` from Overrider's own_field_names, making DiamondOverride silently
    fall through to Base's original value instead of Overrider's override."""

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

    assert DiamondOverride._meta.fields_map["val"].default == 2
