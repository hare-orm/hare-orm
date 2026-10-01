"""The rarely set query settings live in one shared, immutable QueryOptions object per query:
clones share it until one of them changes a setting, and a change never reaches another query."""

import pytest

from hare.query.expressions import F, Q
from hare.query.queryset.query_options import QueryOptions
from hare.query.relation_loading.select import Select
from tests.testmodels import Event, Tournament


def test_plain_querysets_and_their_derived_queries_keep_the_default_options(db):
    queryset = Tournament.objects.filter(name="x").order_by("-id")[:5]
    assert queryset._options is QueryOptions.DEFAULT
    assert Tournament.objects.filter(name="x").order_by("-id").first()._options is QueryOptions.DEFAULT
    # first() of a slice picks its row from the slice - a setting of its own.
    assert queryset.first()._is_single_row_of_slice
    assert queryset.count()._options is QueryOptions.DEFAULT
    assert queryset.exists()._options is QueryOptions.DEFAULT
    assert Tournament.objects.filter(name="x").values("id")._options is QueryOptions.DEFAULT
    assert Tournament.objects.filter(name="x").values_list("id", flat=True)._options is QueryOptions.DEFAULT


def test_setting_on_a_clone_leaves_the_original_alone(db):
    base = Tournament.objects.filter(name="x")
    changed = [
        base.select_for_update(nowait=True),
        base.with_cte("named", Tournament.objects.all()),
        base.alias(double_id=F("id") * 2),
        base.only("id", "name"),
        base.defer("desc"),
        base.order_by("id").after_cursor(1),
        base.group_by("name"),
    ]
    assert base._options is QueryOptions.DEFAULT
    for queryset in changed:
        assert queryset._options is not QueryOptions.DEFAULT
        assert not queryset._options.is_default()
    assert base._select_for_update is False
    assert base._with_ctes == ()
    assert base._alias_keys == frozenset()
    assert base._fields_for_select == ()
    assert base._deferred_fields == ()
    assert base._cursor_values == ()
    assert base._group_bys == ()


def test_sibling_clones_do_not_share_a_changed_setting(db):
    base = Tournament.objects.all()
    first = base.with_cte("first", Tournament.objects.all())
    second = base.with_cte("second", Tournament.objects.all())
    assert [name for name, _query in first._with_ctes] == ["first"]
    assert [name for name, _query in second._with_ctes] == ["second"]
    annotated = base.alias(one=F("id")).annotate(one=F("id"))
    assert annotated._alias_keys == frozenset()
    assert base.alias(one=F("id"))._alias_keys == frozenset({"one"})


def test_select_related_extra_conditions_per_clone(db):
    base = Event.objects.all()
    filtered = base.select_related(Select("tournament", extra_condition=Q(name="a")))
    assert dict(base._select_related_extra_conditions) == {}
    assert set(filtered._select_related_extra_conditions) == {"tournament"}
    assert filtered._explicitly_select_related == frozenset({"tournament"})
    assert base._explicitly_select_related == frozenset()


def test_settings_are_stored_immutable(db):
    queryset = Tournament.objects.all()
    queryset._distinct_on = ["name"]
    queryset._select_for_update_of = {"self"}
    queryset._select_related_extra_conditions = {"key": Q()}
    assert queryset._distinct_on == ("name",)
    assert queryset._select_for_update_of == frozenset({"self"})
    with pytest.raises(AttributeError):
        queryset._with_ctes.append(("name", None))  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        queryset._alias_keys.add("key")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        queryset._select_related_extra_conditions["other"] = Q()  # type: ignore[index]


def test_updated_makes_at_most_one_new_object(db):
    default = QueryOptions.DEFAULT
    assert default.updated(distinct_on=[], alias_keys=None, select_for_update=False) is default
    changed = default.updated(distinct_on=["a"], select_for_update=True, group_bys=("b",))
    assert changed is not default
    assert (changed.distinct_on, changed.select_for_update, changed.group_bys) == (("a",), True, ("b",))
    assert changed.updated(distinct_on=changed.distinct_on) is changed
    assert default.distinct_on == () and default.select_for_update is False
    assert default.is_default() and not changed.is_default()


@pytest.mark.asyncio
async def test_a_changed_setting_still_applies_after_cloning(db):
    await Tournament.objects.create(name="b")
    await Tournament.objects.create(name="a")
    queryset = Tournament.objects.all().only("id", "name")
    ordered = queryset.order_by("name")
    rows = await ordered
    assert [tournament.name for tournament in rows] == ["a", "b"]
    assert all(tournament._partial for tournament in rows)
    assert not any(tournament._partial for tournament in await Tournament.objects.all())
