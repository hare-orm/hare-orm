"""A write of rows on ClickHouse, which has no transactions, cancelled between its statements and its report: the
rows stay written, so the write runs to its end and reports them - whichever write it is."""

import asyncio

import pytest

from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.enums import RowOperation
from hare.instrumentation.observers.observers import Observers
from hare.models.write.write_steps import WriteSteps
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from tests.dialects.clickhouse.models import Player, Skill, Team


async def update_skills():
    await Skill.objects.filter(id__in=[1, 2]).update(name="z")


async def bulk_update_skills():
    skills = await Skill.objects.filter(id__in=[1, 2]).order_by("id")
    for skill in skills:
        skill.name = "z"
    await Skill.objects.bulk_update(skills, ["name"])


async def bulk_create_skills():
    await Skill.objects.bulk_create([Skill(id=3, name="z"), Skill(id=4, name="z")])


@pytest.mark.parametrize("write", [update_skills, bulk_update_skills, bulk_create_skills])
@pytest.mark.asyncio
async def test_a_write_cancelled_before_its_report_still_reports_it(clickhouse_db, monkeypatch, write):
    await Skill.objects.bulk_create([Skill(id=1, name="a"), Skill(id=2, name="b")])
    report = WriteSteps.report
    reporting = asyncio.Event()
    report_released = asyncio.Event()

    async def report_after_a_pause(*args, **kwargs):
        reporting.set()
        await report_released.wait()
        await report(*args, **kwargs)

    monkeypatch.setattr(WriteSteps, "report", staticmethod(report_after_a_pause))
    observed: list[RowsChanged] = []
    with Observers.observing(RowsChanged, observed.append, models=[Skill]):
        task = asyncio.ensure_future(write())
        await reporting.wait()
        task.cancel()
        report_released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.undo()
    assert await Skill.objects.filter(name="z").count() == 2
    assert len(observed) == 1


async def pause_and_cancel(monkeypatch, owner, name, write):
    """Runs ``write`` with ``owner.name`` paused, cancels it there and lets it go on."""
    paused = getattr(owner, name)
    reached = asyncio.Event()
    released = asyncio.Event()

    async def pausing(*args, **kwargs):
        reached.set()
        await released.wait()
        return await paused(*args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(pausing))
    task = asyncio.ensure_future(write())
    await reached.wait()
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_a_delete_cancelled_before_its_report_still_deletes_its_cascade_and_reports_it(
    clickhouse_db, monkeypatch
):
    """An instance's delete ran its cascade, its row and its report as separate steps - a cancel in between
    left the cascade done without the row, or the rows deleted without their report."""
    team = await Team.objects.create(name="t")
    await Player.objects.bulk_create([Player(id=1, name="a", team=team), Player(id=2, name="b", team=team)])
    observed: list[RowsChanged] = []
    with Observers.observing(RowsChanged, observed.append, models=[Team]):
        await pause_and_cancel(monkeypatch, ChangeEvents, "report_deletion", team.delete)
    assert (await Team.objects.count(), await Player.objects.count()) == (0, 0)
    assert [event.operation for event in observed] == [RowOperation.DELETE]


@pytest.mark.asyncio
async def test_a_clear_of_links_cancelled_before_its_report_still_reports_it(clickhouse_db, monkeypatch):
    player = await Player.objects.create(id=1, name="a")
    await Skill.objects.bulk_create([Skill(id=1, name="s"), Skill(id=2, name="t")])
    await player.skills.add(*await Skill.objects.all())
    observed: list[RowsChanged] = []
    with Observers.observing(RowsChanged, observed.append, models=[Player]):
        await pause_and_cancel(monkeypatch, ThroughRows, "report_links", player.skills.clear)
    assert await player.skills.all().count() == 0
    assert len(observed) == 1
