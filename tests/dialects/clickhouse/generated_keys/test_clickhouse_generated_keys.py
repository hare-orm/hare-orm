"""Generated keys on ClickHouse - taken from the model's series of numbers (``generateSerialID``, kept
by ClickHouse Keeper) before the rows are written: by ``create()``, ``save()`` and ``bulk_create()``,
in a transaction too; the series moved past the keys rows were written with; a key beyond its field's
range refused before it is written."""

import pytest

from hare.exceptions import ValidationError
from hare.migrations.operations import SynchronizeKeySeries
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models.write.generated_keys import GeneratedKeys
from tests.dialects.clickhouse.generated_keys.models import Badge, Ticket


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
