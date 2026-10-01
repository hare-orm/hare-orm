"""Correctness tests for ChooseDBMixin.__copy__ (hare/query/queryset/awaitable.py) - the compiled,
per-subclass `newone.a = self.a; ...` copy function `_compiled_copy()` generates from
`_clone_slots()`, replacing a getattr()/setattr() loop for speed (see that file's own
docstrings). This file checks the thing that actually matters if the codegen goes wrong: does the copy carry over
every attribute correctly, independently of the original, for every shape of subclass that
shares this one `__copy__` (QuerySet vs a structurally different sibling like CountQuery)."""

from copy import copy

import pytest

from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_copy_preserves_every_clone_slot_value(db):
    qs = Tournament.objects.filter(name="a").order_by("name").distinct()
    cloned = copy(qs)
    for name in type(qs)._clone_slots():
        original_value = getattr(qs, name)
        assert getattr(cloned, name) == original_value, f"slot {name!r} not preserved by copy"


@pytest.mark.asyncio
async def test_clone_is_independent_of_the_original(db):
    """`__copy__` itself is a plain shallow copy (a mutable slot like `_q_objects` legitimately
    aliases the same list as the original right after it, same as the old getattr/setattr-loop
    implementation) - the independence guarantee for mutable containers comes from `_clone()`
    giving each of them (see its own comment) their own copy on top of `copy(self)`, which is
    what `.filter()`/`.order_by()`/... actually call. Testing that layer, not raw `__copy__`."""
    qs = Tournament.objects.filter(name="a")
    cloned = qs._clone()

    cloned._q_objects.append(Tournament.objects.filter(desc="b")._q_objects[0])
    assert len(cloned._q_objects) == 2
    assert len(qs._q_objects) == 1, "mutating the clone's _q_objects leaked back into the original"

    qs._distinct = True
    assert cloned._distinct is False, "mutating the original after cloning leaked into the clone"


@pytest.mark.asyncio
async def test_copy_works_for_a_structurally_different_sibling_subclass(db):
    """CountQuery shares ChooseDBMixin.__copy__ with QuerySet but declares a different slot
    set (no _orderings/_select_for_update*/etc.) - the exact scenario _clone_slots() being
    per-subclass (not hardcoded to QuerySet) exists to handle correctly."""
    count_query = Tournament.objects.filter(name="a").count()
    cloned = copy(count_query)
    for name in type(count_query)._clone_slots():
        assert getattr(cloned, name) == getattr(count_query, name), f"slot {name!r} not preserved"
    assert cloned is not count_query


def test_compiled_copy_is_cached_per_subclass():
    """_compiled_copy() must not accidentally share ONE cached function across differently-
    shaped subclasses (each subclass's cache lives on its own cls.__dict__, not inherited) -
    QuerySet and CountQuery must each get their own generated function tailored to their own
    _clone_slots()."""
    from hare.query.queryset import QuerySet
    from hare.query.statements import CountQuery

    assert QuerySet._compiled_copy() is not CountQuery._compiled_copy()
