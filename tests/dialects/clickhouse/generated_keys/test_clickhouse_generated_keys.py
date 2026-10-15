"""Generated keys on ClickHouse - taken from the model's series of numbers (``generateSerialID``, kept
by ClickHouse Keeper) before the rows are written: by ``create()``, ``save()`` and ``bulk_create()``,
in a transaction too; the series moved past the keys rows were written with; a key beyond its field's
range refused before it is written."""

import pytest

from hare.exceptions import IntegrityError, ValidationError
from hare.migrations.operations import SynchronizeKeySeries
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models.write.generated_keys import GeneratedKeys
from tests.dialects.clickhouse.generated_keys.models import Badge, BadgeAward, Holder, Ticket


@pytest.mark.asyncio
async def test_keys_are_taken_from_the_series(clickhouse_generated_keys_db):
    connection = Ticket._meta.connection
    first = await Ticket.objects.create(subject="a")
    second = Ticket(subject="b")
    await second.save()
    assert second.pk == first.pk + 1
    created = [Ticket(subject=str(number)) for number in range(5)]
    await Ticket.objects.bulk_create(created)
    keys = [ticket.pk for ticket in created]
    assert keys == list(range(second.pk + 1, second.pk + 6))
    # A key of its own is written as it is. The series of the model is one for every database of the
    # Keeper - its keys go on from wherever another database left it.
    own_key = keys[-1] + 1000
    await Ticket.objects.create(id=own_key, subject="own")
    stored = await Ticket.objects.order_by("id").values_list("id", flat=True)
    assert stored == [first.pk, second.pk, *keys, own_key]
    # A process moves the series past the table's keys before taking its first keys; a migration does
    # it whenever it runs.
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    state.models[("models", "Ticket")] = ModelState.make_from_model("models", Ticket)
    state.add_rendered_model("models", "Ticket")
    await SynchronizeKeySeries("Ticket").database_forward("models", state, state, editor)
    assert (await Ticket.objects.create(subject="next")).pk == own_key + 1


@pytest.mark.asyncio
async def test_a_key_beyond_the_field_is_refused(clickhouse_generated_keys_db):
    connection = Ticket._meta.connection
    await Badge.objects.create(id=2**15 - 1, label="last")
    await connection.synchronize_key_series(Badge)
    with pytest.raises(ValidationError, match="of the series"):
        await Badge.objects.create(label="beyond")
    assert await Badge.objects.count() == 1


@pytest.mark.asyncio
async def test_a_database_generating_keys_as_it_writes_takes_none_before(clickhouse_generated_keys_db, monkeypatch):
    connection = Ticket._meta.connection
    monkeypatch.setattr(connection, "features", connection.features.replace(takes_keys_before_insert=False))
    ticket = Ticket(subject="kept")
    await GeneratedKeys.assign(Ticket, connection, [ticket])
    assert ticket.pk is None


@pytest.mark.asyncio
async def test_rows_added_through_a_model_of_a_generated_key_take_keys_of_its_series(clickhouse_generated_keys_db):
    holder = await Holder.objects.create(id=1)
    badges = [await Badge.objects.create(id=number, label=str(number)) for number in (1, 2, 3)]
    await holder.badges.add(*badges[:2])
    await holder.badges.add(badges[2])
    keys = await BadgeAward.objects.values_list("id", flat=True)
    assert len(set(keys)) == 3 and 0 not in keys
    assert sorted(badge.id for badge in await holder.badges.all()) == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_objects_without_their_keys(clickhouse_generated_keys_db):
    badge = await Badge.objects.create(id=5, label="gold")
    award = BadgeAward(holder_id=999, badge=badge)
    with pytest.raises(IntegrityError, match="no Holder row"):
        await award.save()
    assert award.pk is None
    holder = await Holder.objects.create(id=1)
    awards = [BadgeAward(holder=holder, badge=badge), BadgeAward(holder_id=999, badge=badge)]
    with pytest.raises(IntegrityError, match="no Holder row"):
        await BadgeAward.objects.bulk_create(awards)
    assert [award.pk for award in awards] == [None, None]
    await BadgeAward.objects.bulk_create(awards[:1])
    assert awards[0].pk is not None
