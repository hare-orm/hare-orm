from datetime import UTC

import pytest

from tests.testmodels import (
    BooleanFields,
    DatetimeFields,
    IntFields,
    ManagerModel,
    Tournament,
)


@pytest.mark.asyncio
async def test_native_bucket_fields(db):
    obj = await IntFields.objects.create(intnum=42, intnum_null=None)
    fetched = await IntFields.objects.get(pk=obj.pk)
    assert fetched.intnum == 42
    assert fetched.intnum_null is None


@pytest.mark.asyncio
async def test_default_bucket_fields(db):
    obj = await BooleanFields.objects.create(boolean=True, boolean_null=None)
    fetched = await BooleanFields.objects.get(pk=obj.pk)
    assert fetched.boolean is True
    assert fetched.boolean_null is None


@pytest.mark.asyncio
async def test_complex_bucket_fields(db):
    from datetime import datetime

    dt = datetime(2020, 3, 15, 12, 0, 0, tzinfo=UTC)
    obj = await DatetimeFields.objects.create(datetime=dt, datetime_null=None)
    fetched = await DatetimeFields.objects.get(pk=obj.pk)
    assert fetched.datetime == dt
    assert fetched.datetime_null is None


@pytest.mark.asyncio
async def test_multiple_rows_preserve_identity(db):
    objs = [await IntFields.objects.create(intnum=i) for i in range(5)]
    fetched = await IntFields.objects.all().order_by("intnum")
    assert [f.intnum for f in fetched] == list(range(5))
    assert [f.pk for f in fetched] == [o.pk for o in objs]


@pytest.mark.asyncio
async def test_only_reduced_field_set(db):
    await IntFields.objects.create(intnum=1, intnum_null=2)
    fetched = await IntFields.objects.all().only("intnum")
    assert fetched[0].intnum == 1
    assert fetched[0]._partial is True


@pytest.mark.asyncio
async def test_only_all_fields_matches_full_select(db):
    obj = await IntFields.objects.create(intnum=7, intnum_null=8)
    full = await IntFields.objects.get(pk=obj.pk)
    partial = (await IntFields.objects.filter(pk=obj.pk).only("intnum", "intnum_null"))[0]
    assert full.intnum == partial.intnum == 7
    assert full.intnum_null == partial.intnum_null == 8


@pytest.mark.asyncio
async def test_model_inheritance(db):
    obj = await ManagerModel.objects.create(status=5)
    fetched = await ManagerModel.all_objects.get(pk=obj.pk)
    assert fetched.status == 5


@pytest.mark.asyncio
async def test_annotations_alongside_positional_base_fields(db):
    from hare.query.functions import Count

    tournament = await Tournament.objects.create(name="Cup")
    rows = (
        await Tournament.objects.all()
        .annotate(event_count=Count("events"))
        .filter(pk=tournament.pk)
        .values("id", "name", "event_count")
    )
    assert rows[0]["name"] == "Cup"
    assert rows[0]["event_count"] == 0

    # non-.values() annotate path - annotation attached via row[field] post-processing,
    # base fields still hydrated positionally.
    instances = await Tournament.objects.all().annotate(event_count=Count("events")).filter(pk=tournament.pk)
    assert instances[0].name == "Cup"
    assert instances[0].event_count == 0


@pytest.mark.asyncio
async def test_raw_sql_query_unaffected(db):
    await IntFields.objects.create(intnum=99)
    rows = await IntFields.objects.raw("SELECT * FROM intfields WHERE intnum = 99")
    assert len(rows) == 1
    assert rows[0].intnum == 99


@pytest.mark.asyncio
async def test_union_query_unaffected(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)
    q1 = IntFields.objects.filter(intnum=1)
    q2 = IntFields.objects.filter(intnum=2)
    rows = await q1.union(q2)
    assert sorted(r.intnum for r in rows) == [1, 2]


@pytest.mark.asyncio
async def test_layout_readers_follow_the_bucket_of_each_column(db):
    """A related-model row is read column by column through the layout's readers: none for a
    column the driver already returns as the right type, the field type for a default-bucket
    column (a NULL stays None), the field's own conversion otherwise."""
    from hare import Connections
    from hare.models.enums import FieldBucket

    layout = Tournament._meta.get_hydration_layout(Connections.get("models"))
    for field_name, (column, _field, bucket, _dialect_reader) in layout.entry_by_field_name.items():
        reader = layout.reader_by_field_name[field_name]
        assert layout.reader_by_column[column] is reader
        assert (reader is None) == (bucket == FieldBucket.NATIVE)

    boolean_layout = BooleanFields._meta.get_hydration_layout(Connections.get("models"))
    for field_name, (_column, _field, bucket, _dialect_reader) in boolean_layout.entry_by_field_name.items():
        if bucket == FieldBucket.DEFAULT:
            reader = boolean_layout.reader_by_field_name[field_name]
            assert reader(None) is None
            assert reader(1) is True
