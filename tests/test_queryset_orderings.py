"""QuerySet.orderings - the ordering order_by() gave a queryset, as the query orders by it."""

import pytest

from hare.exceptions import FieldError
from hare.query.expressions import F
from hare.query.functions import Length
from hare.query.queryset.arguments.ordering_arguments import OrderingArguments
from hare.sql import Order
from tests.testmodels import DefaultOrdered, Event, Tournament


def test_an_unordered_queryset_has_no_orderings(db):
    assert Tournament.objects.all().orderings == ()
    assert Tournament.objects.filter(name="x").orderings == ()


def test_order_by_names_each_column_and_direction(db):
    assert Tournament.objects.all().order_by("name", "-id").orderings == (("name", Order.ASC), ("id", Order.DESC))


def test_the_last_order_by_replaces_the_earlier_one(db):
    queryset = Tournament.objects.all().order_by("name").order_by("-created", "id")
    assert queryset.orderings == (("created", Order.DESC), ("id", Order.ASC))


def test_order_by_without_arguments_leaves_no_orderings(db):
    assert Tournament.objects.all().order_by("name").order_by().orderings == ()


def test_an_ordering_expression_keeps_its_null_placement(db):
    queryset = Tournament.objects.all().order_by(F("name").desc(nulls_last=True), F("id").asc(nulls_first=True))
    assert queryset.orderings == (("name", Order.DESC_NULLS_LAST), ("id", Order.ASC_NULLS_FIRST))


def test_pk_and_a_forward_relation_become_their_key_columns(db):
    assert Event.objects.all().order_by("-pk").orderings == (("event_id", Order.DESC),)
    assert Event.objects.all().order_by("tournament", "name").orderings == (
        ("tournament_id", Order.ASC),
        ("name", Order.ASC),
    )
    assert Event.objects.all().order_by("tournament__name").orderings == (("tournament__name", Order.ASC),)


def test_a_clone_keeps_the_orderings(db):
    ordered = Tournament.objects.all().order_by("-name")
    assert ordered.filter(id__gt=1).orderings == (("name", Order.DESC),)
    assert ordered.exclude(name="x").only("id", "name").orderings == (("name", Order.DESC),)
    assert ordered.orderings == (("name", Order.DESC),)


def test_order_by_without_arguments_drops_meta_ordering(db):
    assert "ORDER BY" in DefaultOrdered.objects.all().sql()
    unordered = DefaultOrdered.objects.all().order_by()
    assert "ORDER BY" not in unordered.sql()
    assert "ORDER BY" not in unordered.filter(second__gt=1).sql()
    assert "ORDER BY" in unordered.order_by("one").sql()


def test_meta_ordering_is_not_included(db):
    assert DefaultOrdered._meta.ordering
    assert DefaultOrdered.objects.all().orderings == ()
    assert DefaultOrdered.objects.all().order_by("-second").orderings == (("second", Order.DESC),)


def test_cursors_keep_the_orderings_as_given(db):
    ordered = Tournament.objects.all().order_by("name", "-id")
    expected = (("name", Order.ASC), ("id", Order.DESC))
    assert ordered.after_cursor("m", 5).orderings == expected
    assert ordered.before_cursor("m", 5).orderings == expected
    assert ordered.after_cursor("a", 9).before_cursor("m", 5).orderings == expected


@pytest.mark.asyncio
async def test_the_rows_come_in_the_reported_order(db):
    for name in ("b", "a", "c"):
        await Tournament.objects.create(name=name)
    queryset = Tournament.objects.all().order_by("-name")
    ((path, order),) = queryset.orderings
    names = await queryset.values_list(path, flat=True)
    assert names == sorted(names, reverse=not order.is_ascending)


def test_field_names_given_as_text_are_parsed_once(db):
    OrderingArguments.plain_orderings.forget_all()
    first = Event.objects.all().order_by("tournament", "-name")
    kept = OrderingArguments.plain_orderings.get_for_model(Event, (("tournament", "-name"),))
    assert kept == (("tournament_id", Order.ASC), ("name", Order.DESC))
    second = Event.objects.filter(name="x").order_by("tournament", "-name")
    assert first.orderings == second.orderings == kept
    # Each queryset holds a list of its own.
    assert first._orderings is not second._orderings


def test_an_ordering_expression_is_parsed_on_every_call(db):
    OrderingArguments.plain_orderings.forget_all()
    Tournament.objects.all().order_by(F("name").desc(), "id")
    assert OrderingArguments.get_plain_orderings(Tournament.objects.all(), (F("name").desc(), "id")) is None
    assert len(OrderingArguments.plain_orderings) == 0


def test_an_unknown_name_is_refused_on_every_call(db):
    for _ in range(2):
        with pytest.raises(FieldError):
            Tournament.objects.all().order_by("no_such_field")


def test_a_name_of_an_annotation_orders_by_the_annotation(db):
    Tournament.objects.all().order_by("name")
    queryset = Tournament.objects.annotate(name_length=Length("name")).order_by("name_length", "name")
    assert queryset.orderings == (("name_length", Order.ASC), ("name", Order.ASC))
    assert OrderingArguments.plain_orderings.get_for_model(Tournament, (("name_length", "name"),)) is None
