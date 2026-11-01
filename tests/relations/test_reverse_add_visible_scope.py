"""A reverse foreign key's add() refuses a row the related model's default scope hides - on a database
reporting no count of the rows an UPDATE changed (ClickHouse) too, where the row went unwritten unnoticed
and a RowsChanged named it."""

import dataclasses

import pytest

from hare.exceptions import IntegrityError
from hare.instrumentation import Observers, RowsChanged
from tests.shared_models.testmodels import SoftDeleteChildCascadeSoft, SoftDeleteParent


@pytest.mark.parametrize("reports_written_row_count", [True, False], ids=["row count", "no row count"])
@pytest.mark.asyncio
async def test_add_refuses_a_soft_deleted_row(db, monkeypatch, reports_written_row_count):
    connection = SoftDeleteParent._meta.connection
    monkeypatch.setattr(
        connection,
        "features",
        dataclasses.replace(connection.features, reports_written_row_count=reports_written_row_count),
    )
    first = await SoftDeleteParent.objects.create(name="first")
    second = await SoftDeleteParent.objects.create(name="second")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=second)
    await child.delete()
    events = []
    observer = Observers.observe(RowsChanged, events.append)
    try:
        with pytest.raises(IntegrityError, match="isn't shown by its default scope"):
            await first.cascade_children.add(child)
    finally:
        Observers.unobserve(RowsChanged, observer)
    stored = await SoftDeleteChildCascadeSoft.objects.include_deleted().get(id=child.id)
    assert stored.parent_id == second.id
    assert events == []
