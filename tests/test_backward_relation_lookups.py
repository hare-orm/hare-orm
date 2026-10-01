"""Direct lookups (`isnull`/equality/`in`/`not`/`not_in`) on a backward FK/O2O relation."""

import pytest

from hare.models import Model
from tests.testmodels import Address, Author, Employee, Event, O2oPkModelWithM2m, SourceFields, Tournament


async def _create_events_with_one_address(name_prefix: str = "") -> tuple[Event, Event]:
    tournament = await Tournament.objects.create(name=f"{name_prefix}T")
    event_with_address = await Event.objects.create(name=f"{name_prefix}with", tournament=tournament)
    event_without_address = await Event.objects.create(name=f"{name_prefix}without", tournament=tournament)
    await Address.objects.create(city="C", street="S", event=event_with_address)
    return event_with_address, event_without_address


@pytest.mark.asyncio
async def test_backward_o2o_pk_owner_isnull_true(db):
    """`Address.event` is both a OneToOneField and Address's own primary key - the backward
    `Event.address` lookups used to read Address's pk_attr before it was switched from the field
    name ("event") to the real column shadow ("event_id"), querying a column that doesn't exist."""
    _event_with_address, event_without_address = await _create_events_with_one_address()

    assert [event.pk async for event in Event.objects.filter(address__isnull=True)] == [event_without_address.pk]


@pytest.mark.asyncio
async def test_backward_o2o_pk_owner_isnull_false_and_not_isnull(db):
    event_with_address, event_without_address = await _create_events_with_one_address()

    assert [event.pk async for event in Event.objects.filter(address__isnull=False)] == [event_with_address.pk]
    assert [event.pk async for event in Event.objects.filter(address__not_isnull=True)] == [event_with_address.pk]
    assert [event.pk async for event in Event.objects.filter(address__not_isnull=False)] == [event_without_address.pk]


@pytest.mark.asyncio
async def test_backward_o2o_pk_owner_equality_in_not_and_not_in(db):
    event_with_address, event_without_address = await _create_events_with_one_address()
    address = await Address.objects.get(event=event_with_address)

    assert [event.pk async for event in Event.objects.filter(address=address.pk)] == [event_with_address.pk]
    assert [event.pk async for event in Event.objects.filter(address__in=[address.pk])] == [event_with_address.pk]
    assert sorted([event.pk async for event in Event.objects.filter(address__not=address.pk)]) == sorted(
        [event_without_address.pk]
    )
    assert [event.pk async for event in Event.objects.filter(address__not_in=[address.pk])] == [
        event_without_address.pk
    ]


@pytest.mark.asyncio
async def test_backward_o2o_pk_owner_filters_compare_the_real_pk_column(db):
    quoted_pk_column = Event.get_connection().dialect.quote_identifier("event_id")
    for lookup_name, value in (
        ("address__isnull", True),
        ("address", 1),
        ("address__not", 1),
        ("address__in", [1]),
        ("address__not_in", [1]),
    ):
        where_sql = Event.objects.filter(**{lookup_name: value}).sql().split(" WHERE ", 1)[1]
        assert quoted_pk_column in where_sql


@pytest.mark.asyncio
async def test_backward_o2o_pk_owner_second_model_isnull(db):
    """Same shape on a second, unrelated O2O-pk model."""
    author_with_row = await Author.objects.create(name="with")
    author_without_row = await Author.objects.create(name="without")
    await O2oPkModelWithM2m.objects.create(author=author_with_row)

    assert [author.pk async for author in Author.objects.filter(o2opkmodelwithm2ms__isnull=True)] == [
        author_without_row.pk
    ]
    assert [author.pk async for author in Author.objects.filter(o2opkmodelwithm2ms__isnull=False)] == [
        author_with_row.pk
    ]


def _pks(models: list[Model]) -> list[int]:
    return sorted(model.pk for model in models)


@pytest.mark.asyncio
async def test_self_referential_backward_fk_isnull(db):
    boss = await Employee.objects.create(name="Boss")
    mid = await Employee.objects.create(name="Mid", manager=boss)
    leaf = await Employee.objects.create(name="Leaf", manager=mid)

    assert _pks(await Employee.objects.filter(team_members__isnull=True)) == [leaf.pk]
    assert _pks(await Employee.objects.filter(team_members__isnull=False)) == _pks([boss, mid])
    assert _pks(await Employee.objects.filter(team_members__not_isnull=True)) == _pks([boss, mid])
    assert _pks(await Employee.objects.filter(team_members__not_isnull=False)) == [leaf.pk]


@pytest.mark.asyncio
async def test_self_referential_backward_fk_equality_in_not_and_not_in(db):
    boss = await Employee.objects.create(name="Boss")
    mid = await Employee.objects.create(name="Mid", manager=boss)
    leaf = await Employee.objects.create(name="Leaf", manager=mid)

    assert _pks(await Employee.objects.filter(team_members=mid.pk)) == [boss.pk]
    assert _pks(await Employee.objects.filter(team_members__in=[mid.pk, leaf.pk])) == _pks([boss, mid])
    assert _pks(await Employee.objects.filter(team_members__not=mid.pk)) == _pks([mid, leaf])
    assert _pks(await Employee.objects.filter(team_members__not_in=[mid.pk])) == _pks([mid, leaf])


@pytest.mark.asyncio
async def test_self_referential_backward_fk_equals_none(db):
    boss = await Employee.objects.create(name="Boss")
    leaf = await Employee.objects.create(name="Leaf", manager=boss)

    assert _pks(await Employee.objects.filter(team_members=None)) == [leaf.pk]


@pytest.mark.asyncio
async def test_self_referential_backward_fk_with_source_field_column_names(db):
    root = await SourceFields.objects.create(chars="root")
    child = await SourceFields.objects.create(chars="child", fk=root)

    assert _pks(await SourceFields.objects.filter(fkrev__isnull=True)) == [child.pk]
    assert _pks(await SourceFields.objects.filter(fkrev=child.pk)) == [root.pk]
    assert _pks(await SourceFields.objects.filter(fkrev__in=[child.pk])) == [root.pk]
