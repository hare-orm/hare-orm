"""update(...).returning(...) / delete().returning(...) / hard_delete().returning(...): the rows a
write wrote - named fields as dicts or the model instances - through one statement's RETURNING or
read in the write's transaction (a soft delete, a cascade in Python), and what they refuse."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hare.exceptions import FieldError, UnSupportedError
from hare.query.expressions import F
from tests.testmodels import (
    CompositePkThing,
    DeletePreviewChapter,
    DeletePreviewPage,
    DeletePreviewSoftFolder,
    DeletePreviewSoftNote,
    Event,
    ExpressionTypeParity,
    IntFields,
    SoftDeleteAutoNow,
    Tournament,
    UpsertTarget,
)


async def create_int_rows() -> None:
    for row_id in range(1, 6):
        await IntFields.objects.create(id=row_id, intnum=row_id * 10)


@pytest.mark.asyncio
async def test_update_returns_the_named_fields_as_written(db):
    await create_int_rows()
    rows = await IntFields.objects.filter(id__gte=4).update(intnum=F("intnum") + 1).returning("id", "intnum")
    assert sorted(rows, key=lambda row: row["id"]) == [{"id": 4, "intnum": 41}, {"id": 5, "intnum": 51}]
    assert await IntFields.objects.filter(id=4).values_list("intnum", flat=True) == [41]
    assert await IntFields.objects.filter(id=99).update(intnum=1).returning("id") == []
    assert await IntFields.objects.none().update(intnum=1).returning("id") == []
    keys = await IntFields.objects.filter(id=1).update(intnum_null=7).returning("pk")
    assert keys == [{"pk": 1}]


@pytest.mark.asyncio
async def test_update_returns_the_model_instances(db):
    await create_int_rows()
    rows = await IntFields.objects.filter(id__in=[2, 3]).update(intnum_null=F("intnum") * 2).returning()
    assert sorted((row.id, row.intnum, row.intnum_null) for row in rows) == [(2, 20, 40), (3, 30, 60)]
    assert all(isinstance(row, IntFields) for row in rows)
    # A saved instance - writable as any other.
    rows[0].intnum = 1
    await rows[0].save()
    assert await IntFields.objects.filter(id=rows[0].id).values_list("intnum", flat=True) == [1]


@pytest.mark.asyncio
async def test_update_through_a_join_a_slice_and_a_relation(db):
    first = await Tournament.objects.create(id=1, name="First")
    second = await Tournament.objects.create(id=2, name="Second")
    for event_id, tournament in [(1, first), (2, first), (3, second)]:
        await Event.objects.create(event_id=event_id, tournament=tournament, name=f"event {event_id}")
    joined = (
        await Event.objects.filter(tournament__name="First").update(name="renamed").returning("event_id", "tournament")
    )
    assert sorted(joined, key=lambda row: row["event_id"]) == [
        {"event_id": 1, "tournament": 1},
        {"event_id": 2, "tournament": 1},
    ]
    moved = await Event.objects.filter(event_id=3).update(tournament=first).returning("tournament", "name")
    assert moved == [{"tournament": 1, "name": "event 3"}]
    sliced = await Event.objects.order_by("-event_id").limit(1).update(name="last").returning("event_id")
    assert sliced == [{"event_id": 3}]


@pytest.mark.asyncio
async def test_update_returns_the_auto_now_and_version_values(db):
    row = await SoftDeleteAutoNow.objects.create(name="a")
    rows = await SoftDeleteAutoNow.objects.filter(id=row.id).update(name="b").returning("name", "updated_at")
    assert rows[0]["name"] == "b"
    assert rows[0]["updated_at"] >= row.updated_at
    target = await UpsertTarget.objects.create(id=1, code="x")
    versions = await UpsertTarget.objects.filter(id=1).update(note="y").returning("version", "note")
    assert versions == [{"version": target.version + 1, "note": "y"}]


@pytest.mark.asyncio
async def test_a_composite_primary_key(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="a")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="b")
    rows = await CompositePkThing.objects.filter(thing_id=1).update(name="c").returning("pk", "name")
    assert sorted(rows, key=lambda row: row["pk"]) == [{"pk": (1, 1), "name": "c"}, {"pk": (1, 2), "name": "c"}]
    instances = await CompositePkThing.objects.filter(revision=2).update(name="d").returning()
    assert [(instance.pk, instance.name) for instance in instances] == [((1, 2), "d")]
    deleted = await CompositePkThing.objects.filter(revision=1).delete().returning("thing_id", "revision")
    assert deleted == [{"thing_id": 1, "revision": 1}]


@pytest.mark.asyncio
async def test_an_expression_value_checked_after_the_write(db):
    row = await ExpressionTypeParity.objects.create(
        num=1, big=2, dec=Decimal("1.50"), flag=True, grp="a", ts=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    )
    rows = await ExpressionTypeParity.objects.filter(id=row.id).update(dec=F("dec") + Decimal("1.25")).returning("dec")
    assert rows == [{"dec": Decimal("2.75")}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field_names", [("id",), ("id", "intnum"), ()])
async def test_the_same_update_again_returns_the_new_rows(db, field_names):
    await create_int_rows()
    for row_id in (1, 2, 1):
        rows = await IntFields.objects.filter(id=row_id).update(intnum=F("intnum") + 1).returning(*field_names)
        assert len(rows) == 1
        returned_id = rows[0].id if not field_names else rows[0]["id"]
        assert returned_id == row_id


@pytest.mark.asyncio
async def test_delete_returns_the_deleted_rows(db):
    await create_int_rows()
    rows = await IntFields.objects.filter(id__lte=2).delete().returning("id", "intnum")
    assert sorted(rows, key=lambda row: row["id"]) == [{"id": 1, "intnum": 10}, {"id": 2, "intnum": 20}]
    instances = await IntFields.objects.filter(id=3).delete().returning()
    assert [(instance.id, instance.intnum) for instance in instances] == [(3, 30)]
    assert await IntFields.objects.order_by("id").values_list("id", flat=True) == [4, 5]
    assert await IntFields.objects.filter(id=99).delete().returning("id") == []


@pytest.mark.asyncio
async def test_a_soft_delete_returns_the_rows_as_deleted(db):
    for name in ("a", "b", "c"):
        await SoftDeleteAutoNow.objects.create(name=name)
    rows = await SoftDeleteAutoNow.objects.filter(name__in=["a", "b"]).delete().returning("name", "deleted_at")
    assert sorted(row["name"] for row in rows) == ["a", "b"]
    assert all(isinstance(row["deleted_at"], datetime.datetime) for row in rows)
    # Already deleted - not deleted again, not returned.
    again = await SoftDeleteAutoNow.objects.include_deleted().filter(name__in=["a", "c"]).delete().returning("name")
    assert again == [{"name": "c"}]
    hard = await SoftDeleteAutoNow.objects.include_deleted().filter(name="a").hard_delete().returning()
    assert [(row.name, row.deleted_at is not None) for row in hard] == [("a", True)]
    assert await SoftDeleteAutoNow.objects.include_deleted().count() == 2


@pytest.mark.asyncio
async def test_a_soft_delete_with_a_cascade(db):
    folder = await DeletePreviewSoftFolder.objects.create(name="folder")
    await DeletePreviewSoftNote.objects.create(name="note", folder=folder)
    rows = await DeletePreviewSoftFolder.objects.filter(id=folder.id).delete().returning()
    assert [(row.name, row.deleted_at is not None) for row in rows] == [("folder", True)]
    assert await DeletePreviewSoftNote.objects.count() == 0


@pytest.mark.asyncio
async def test_a_hard_delete_with_a_cascade_in_python(db):
    from tests.testmodels import DeletePreviewAuthor, DeletePreviewBook

    author = await DeletePreviewAuthor.objects.create(name="author")
    book = await DeletePreviewBook.objects.create(name="book", author=author)
    chapter = await DeletePreviewChapter.objects.create(name="chapter", book=book)
    await DeletePreviewPage.objects.create(name="page", chapter=chapter)
    rows = await DeletePreviewChapter.objects.filter(id=chapter.id).delete().returning("name", "book")
    assert rows == [{"name": "chapter", "book": book.id}]
    assert await DeletePreviewPage.objects.count() == 0


@pytest.mark.parametrize(
    "field_names",
    [("missing",), ("tournament__name",), ("events",), ("id", "id"), (5,)],
    ids=["unknown", "a path", "a reverse relation", "twice", "not a name"],
)
def test_a_wrong_field_is_refused(field_names):
    with pytest.raises(FieldError):
        Tournament.objects.filter(id=1).update(name="x").returning(*field_names)
    with pytest.raises(FieldError):
        Tournament.objects.filter(id=1).delete().returning(*field_names)


@pytest.mark.asyncio
async def test_a_database_without_returning_refuses_it(db, monkeypatch):
    await create_int_rows()
    connection = IntFields.get_connection()
    monkeypatch.setattr(connection, "features", connection.features.replace(supports_returning=False))
    with pytest.raises(UnSupportedError, match="update\\(\\).returning\\(\\) needs RETURNING"):
        await IntFields.objects.filter(id=1).update(intnum=1).returning("id")
    with pytest.raises(UnSupportedError, match="delete\\(\\).returning\\(\\) needs RETURNING"):
        await IntFields.objects.filter(id=1).delete().returning("id")
    monkeypatch.undo()
    assert await IntFields.objects.count() == 5
