from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import (
    QueryError,
)
from hare.models.write.instance_writer import InstanceWriter
from hare.query.expressions import Case, F, Function, OuterReference, Q, Subquery, When
from hare.query.functions import Sum, Upper
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.sql.terms import Function as HareSqlFunction
from hare.time import UTC, Timezone
from tests.testmodels import (
    Author,
    Book,
    CompositePkThing,
    CompositePkTriple,
    Currency,
    DatetimeFields,
    DefaultUpdate,
    EnumFields,
    Event,
    IntFields,
    JSONFields,
    NumberSourceField,
    Reporter,
    Service,
    SmallIntFields,
    SourceFieldPk,
    Tournament,
    UUIDFields,
    VersionedThing,
)


@pytest.mark.asyncio
async def test_update(db):
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="3")
    rows_affected = await Tournament.objects.all().update(name="2")
    assert rows_affected == 2

    tournament = await Tournament.objects.first()
    assert tournament.name == "2"


@pytest.mark.asyncio
async def test_bulk_update(db):
    objs = [await Tournament.objects.create(name="1"), await Tournament.objects.create(name="2")]
    objs[0].name = "0"
    objs[1].name = "1"
    rows_affected = await Tournament.objects.bulk_update(objs, fields=["name"], batch_size=100)
    assert rows_affected == 2
    assert (await Tournament.objects.get(pk=objs[0].pk)).name == "0"
    assert (await Tournament.objects.get(pk=objs[1].pk)).name == "1"


@pytest.mark.asyncio
async def test_bulk_update_duplicate_pk_raises(db):
    """bulk_update() builds one UPDATE ... FROM (VALUES ...) statement per chunk - two objects
    sharing the same pk produce two VALUES rows matching the SAME target row, an ambiguous shape
    Postgres itself only resolves by applying an UNSPECIFIED one of them. Must be rejected
    up front instead of silently dropping one of the two intended updates.

    Two INDEPENDENT Python objects (not the same instance twice) loaded from the same row -
    the realistic way this happens (e.g. two different code paths each fetching "the current
    row" before bulk_update() is called once over the combined list)."""
    created = await Tournament.objects.create(name="1")
    first_copy = await Tournament.objects.get(pk=created.pk)
    second_copy = await Tournament.objects.get(pk=created.pk)
    first_copy.name = "first-update"
    second_copy.name = "second-update"
    with pytest.raises(QueryError, match="same primary key"):
        await Tournament.objects.bulk_update([first_copy, second_copy], fields=["name"])


@pytest.mark.asyncio
async def test_bulk_update_multiple_fields(db):
    objs = [
        await IntFields.objects.create(intnum=1, intnum_null=10),
        await IntFields.objects.create(intnum=2, intnum_null=20),
        await IntFields.objects.create(intnum=3, intnum_null=30),
    ]
    for idx, obj in enumerate(objs):
        obj.intnum = 100 + idx
        obj.intnum_null = 200 + idx
    rows_affected = await IntFields.objects.bulk_update(objs, fields=["intnum", "intnum_null"])
    assert rows_affected == 3
    for idx, obj in enumerate(objs):
        refreshed = await IntFields.objects.get(pk=obj.pk)
        assert refreshed.intnum == 100 + idx
        assert refreshed.intnum_null == 200 + idx


@pytest.mark.asyncio
async def test_bulk_update_datetime(db):
    objs = [
        await DatetimeFields.objects.create(datetime=datetime(2021, 1, 1, tzinfo=UTC)),
        await DatetimeFields.objects.create(datetime=datetime(2021, 1, 1, tzinfo=UTC)),
    ]
    t0 = datetime(2021, 1, 2, tzinfo=UTC)
    t1 = datetime(2021, 1, 3, tzinfo=UTC)
    objs[0].datetime = t0
    objs[1].datetime = t1
    rows_affected = await DatetimeFields.objects.bulk_update(objs, fields=["datetime"])
    assert rows_affected == 2
    assert (await DatetimeFields.objects.get(pk=objs[0].pk)).datetime == t0
    assert (await DatetimeFields.objects.get(pk=objs[1].pk)).datetime == t1


@pytest.mark.asyncio
async def test_bulk_update_pk_non_id(db):
    tournament = await Tournament.objects.create(name="")
    events = [
        await Event.objects.create(name="1", tournament=tournament),
        await Event.objects.create(name="2", tournament=tournament),
    ]
    events[0].name = "3"
    events[1].name = "4"
    rows_affected = await Event.objects.bulk_update(events, fields=["name"])
    assert rows_affected == 2
    assert (await Event.objects.get(pk=events[0].pk)).name == events[0].name
    assert (await Event.objects.get(pk=events[1].pk)).name == events[1].name


@pytest.mark.asyncio
async def test_bulk_update_pk_uuid(db):
    objs = [
        await UUIDFields.objects.create(data=uuid.uuid4()),
        await UUIDFields.objects.create(data=uuid.uuid4()),
    ]
    objs[0].data = uuid.uuid4()
    objs[1].data = uuid.uuid4()
    rows_affected = await UUIDFields.objects.bulk_update(objs, fields=["data"])
    assert rows_affected == 2
    assert (await UUIDFields.objects.get(pk=objs[0].pk)).data == objs[0].data
    assert (await UUIDFields.objects.get(pk=objs[1].pk)).data == objs[1].data


@pytest.mark.asyncio
async def test_bulk_renamed_pk_source_field(db):
    objs = [
        await SourceFieldPk.objects.create(name="Model 1"),
        await SourceFieldPk.objects.create(name="Model 2"),
    ]
    objs[0].name = "Model 3"
    objs[1].name = "Model 4"
    rows_affected = await SourceFieldPk.objects.bulk_update(objs, fields=["name"])
    assert rows_affected == 2
    assert (await SourceFieldPk.objects.get(pk=objs[0].pk)).name == objs[0].name
    assert (await SourceFieldPk.objects.get(pk=objs[1].pk)).name == objs[1].name


@pytest.mark.asyncio
async def test_bulk_update_json_value(db):
    objs = [
        await JSONFields.objects.create(data={}),
        await JSONFields.objects.create(data={}),
    ]
    objs[0].data = [0]
    objs[1].data = {"a": 1}
    rows_affected = await JSONFields.objects.bulk_update(objs, fields=["data"])
    assert rows_affected == 2
    assert (await JSONFields.objects.get(pk=objs[0].pk)).data == objs[0].data
    assert (await JSONFields.objects.get(pk=objs[1].pk)).data == objs[1].data


@pytest.mark.asyncio
async def test_bulk_update_smallint_none(db):
    objs = [
        await SmallIntFields.objects.create(smallintnum=1, smallintnum_null=1),
        await SmallIntFields.objects.create(smallintnum=2, smallintnum_null=2),
    ]
    objs[0].smallintnum_null = None
    objs[1].smallintnum_null = None
    rows_affected = await SmallIntFields.objects.bulk_update(objs, fields=["smallintnum_null"])
    assert rows_affected == 2
    assert (await SmallIntFields.objects.get(pk=objs[0].pk)).smallintnum_null is None
    assert (await SmallIntFields.objects.get(pk=objs[1].pk)).smallintnum_null is None


@pytest.mark.asyncio
async def test_bulk_update_custom_field(db):
    objs = [
        await EnumFields.objects.create(service=Service.python_programming, currency=Currency.EUR),
        await EnumFields.objects.create(service=Service.database_design, currency=Currency.USD),
    ]
    objs[0].currency = Currency.USD
    objs[1].service = Service.system_administration
    rows_affected = await EnumFields.objects.bulk_update(objs, fields=["service", "currency"])
    assert rows_affected == 2
    assert (await EnumFields.objects.get(pk=objs[0].pk)).currency == Currency.USD
    assert (await EnumFields.objects.get(pk=objs[1].pk)).service == Service.system_administration


@pytest.mark.asyncio
async def test_bulk_update_source_field_regular_field(db):
    """A regular (non-pk) field with a custom source_field must be updated through its
    actual DB column, not the Python attribute name."""
    objs = [
        await NumberSourceField.objects.create(number=1),
        await NumberSourceField.objects.create(number=2),
    ]
    objs[0].number = 10
    objs[1].number = 20
    rows_affected = await NumberSourceField.objects.bulk_update(objs, fields=["number"])
    assert rows_affected == 2
    assert (await NumberSourceField.objects.get(pk=objs[0].pk)).number == 10
    assert (await NumberSourceField.objects.get(pk=objs[1].pk)).number == 20


@pytest.mark.asyncio
async def test_bulk_update_bumps_auto_now_field(db):
    """bulk_update() must bump an auto_now field the same way save() does, even when the
    caller's fields= list didn't name it - silently skipping it would leave it stale."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    original_modified = event.modified
    event.name = "changed"
    await Event.objects.bulk_update([event], fields=["name"])
    refreshed = await Event.objects.get(pk=event.pk)
    assert refreshed.name == "changed"
    assert refreshed.modified > original_modified


@pytest.mark.asyncio
async def test_queryset_update_bumps_auto_now_field(db):
    """QuerySet.update() must bump an auto_now field the same way save()/bulk_update() do, even
    when the caller's update_kwargs didn't name it - it was the one write path that never got
    this: Model.save(update_fields=[...]) bumps it (execute_update() force-appends every
    auto_now field), bulk_update() bumps it too (QuerySet.bulk_update() injects it into fields=),
    but .filter(...).update(...) left it stale."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    original_modified = event.modified

    rows_affected = await Event.objects.filter(pk=event.pk).update(alias=2)
    assert rows_affected == 1

    refreshed = await Event.objects.get(pk=event.pk)
    assert refreshed.alias == 2
    assert refreshed.modified > original_modified


@pytest.mark.asyncio
async def test_bulk_update_forward_fk_field(db):
    """A forward FK field name must update the real FK column (matching what .update() and
    save() both already support), not crash trying to convert the related Model instance
    itself into the column's DB value."""
    tournament1 = await Tournament.objects.create(name="t1")
    tournament2 = await Tournament.objects.create(name="t2")
    event = await Event.objects.create(name="e", tournament=tournament1)
    event.tournament = tournament2
    rows_affected = await Event.objects.bulk_update([event], fields=["tournament"])
    assert rows_affected == 1
    refreshed = await Event.objects.get(pk=event.pk)
    assert refreshed.tournament_id == tournament2.pk


@pytest.mark.asyncio
async def test_bulk_update_rejects_m2m_field(db):
    """A many-to-many field has no column on this model's own table for the VALUES-table
    UPDATE to target - reject clearly instead of crashing or silently doing nothing."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    with pytest.raises(QueryError, match="bulk_update.. doesn't support relation field"):
        await Event.objects.bulk_update([event], fields=["participants"])


@pytest.mark.asyncio
async def test_bulk_update_multiple_batches(db):
    """batch_size smaller than the object count must split into multiple queries whose
    row counts get summed, not just execute a single query for everything."""
    objs = [await IntFields.objects.create(intnum=i) for i in range(5)]
    for obj in objs:
        obj.intnum = obj.intnum + 100
    rows_affected = await IntFields.objects.bulk_update(objs, fields=["intnum"], batch_size=2)
    assert rows_affected == 5
    for i, obj in enumerate(objs):
        assert (await IntFields.objects.get(pk=obj.pk)).intnum == i + 100


@pytest.mark.asyncio
async def test_bulk_update_empty_objects(db):
    rows_affected = await IntFields.objects.bulk_update([], fields=["intnum"])
    assert rows_affected == 0


@pytest.mark.asyncio
async def test_bulk_update_with_filter(db):
    """QuerySet.filter().bulk_update() must AND the filter's WHERE condition together with
    the VALUES-table join condition, not silently drop one or the other."""
    objs = [
        await IntFields.objects.create(intnum=1, intnum_null=1),
        await IntFields.objects.create(intnum=2, intnum_null=2),
    ]
    objs[0].intnum_null = 100
    objs[1].intnum_null = 200
    # Filter excludes objs[1] - only objs[0] should actually be updated.
    rows_affected = await IntFields.objects.filter(intnum=1).bulk_update(objs, fields=["intnum_null"])
    assert rows_affected == 1
    assert (await IntFields.objects.get(pk=objs[0].pk)).intnum_null == 100
    assert (await IntFields.objects.get(pk=objs[1].pk)).intnum_null == 2


@pytest.mark.asyncio
async def test_bulk_update_with_relation_crossing_filter(db):
    """BulkUpdateQuery._make_queries() used to build self.query as an UPDATE-shaped builder,
    resolve the relation-crossing filter's JOIN onto it, then only carry self.query._wheres
    (not the JOIN itself) into the hand-built raw `UPDATE ... FROM (VALUES ...) WHERE ...` SQL
    text - the WHERE criterion referenced a joined alias with nothing in that raw SQL's FROM
    clause to actually join it, crashing with "no such column"/"missing FROM-clause entry"."""
    author1 = await Author.objects.create(name="A1")
    author2 = await Author.objects.create(name="A2")
    book1 = await Book.objects.create(name="B1", author=author1, rating=1.0)
    book2 = await Book.objects.create(name="B2", author=author2, rating=1.0)

    book1.rating = 5.0
    book2.rating = 5.0
    # Filter crosses the author relation - only book1 (author1's book) should be updated.
    rows_affected = await Book.objects.filter(author__name="A1").bulk_update([book1, book2], fields=["rating"])
    assert rows_affected == 1
    assert (await Book.objects.get(pk=book1.pk)).rating == 5.0
    assert (await Book.objects.get(pk=book2.pk)).rating == 1.0


@pytest.mark.asyncio
async def test_bulk_update_uses_serialize_instances_for_set_values(db):
    """The VALUES-table SET-column values must be computed via the shared serialize_instances()
    helper (the same serialize_rows()-backed path BulkCreateQuery already uses), not a per-field
    field.to_db_value() Python loop - without changing the query's single-round-trip shape or its
    accurate per-call affected-row count (a per-object bind-param loop instead of one
    multi-row UPDATE per chunk measured 33x slower against tortoise-orm)."""
    objs = [await Tournament.objects.create(name="1"), await Tournament.objects.create(name="2")]
    objs[0].name = "0"
    objs[1].name = "1"
    with (
        patch(
            "hare.query.statements.write.bulk.bulk_write_batches.BulkWriteBatches.serialize_instances",
            wraps=BulkWriteBatches.serialize_instances,
        ) as spied_serialize,
        patch(
            "hare.query.statements.write.bulk.bulk_write_batches.BulkWriteBatches.write_statement_parameters",
            wraps=BulkWriteBatches.write_statement_parameters,
        ) as spied_parameters,
    ):
        rows_affected = await Tournament.objects.bulk_update(objs, fields=["name"])
    assert rows_affected == 2
    # One batch serialization: the driver's own parameters where it binds them, else Python rows.
    written_by_driver = (
        Tournament.get_connection().features.binds_written_parameters and HydrateAccelerator.module is not None
    )
    spied_parameters.assert_called_once()
    assert spied_serialize.call_count == (0 if written_by_driver else 1)
    assert (await Tournament.objects.get(pk=objs[0].pk)).name == "0"
    assert (await Tournament.objects.get(pk=objs[1].pk)).name == "1"


@pytest.mark.asyncio
async def test_bulk_update_with_optimistic_lock_field_still_stale_checks(db):
    """A model with Meta.optimistic_lock_field must keep its existing per-row stale-detection - the
    serialize_instances() change only affects how SET-column values are computed, never the
    VALUES-table/stale-check machinery itself."""
    objs = [await VersionedThing.objects.create(name="1"), await VersionedThing.objects.create(name="2")]
    objs[0].name = "0"
    rows_affected = await VersionedThing.objects.bulk_update(objs, fields=["name"])
    assert rows_affected == 2


@pytest.mark.asyncio
async def test_update_auto_now(db):
    obj = await DefaultUpdate.objects.create()

    updated_at = Timezone.now() - timedelta(days=1)
    await DefaultUpdate.objects.filter(pk=obj.pk).update(updated_at=updated_at)

    obj1 = await DefaultUpdate.objects.get(pk=obj.pk)
    assert obj1.updated_at.date() == updated_at.date()


@pytest.mark.asyncio
async def test_update_relation(db):
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")

    await Event.objects.create(name="1", tournament=tournament_first)
    await Event.objects.all().update(tournament=tournament_second)
    event = await Event.objects.first()
    assert event.tournament_id == tournament_second.id


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_update_with_custom_function(db):
    class JsonSet(Function):
        class HareSqlJsonSet(HareSqlFunction):
            def __init__(self, field: F, expression: str, value: Any):
                super().__init__("JSON_SET", field, expression, value)

        database_function = HareSqlJsonSet

    json = await JSONFields.objects.create(data={})
    assert json.data_default == {"a": 1}

    json.data_default = JsonSet(F("data_default"), "$.a", 2)
    await json.save()

    json_update = await JSONFields.objects.get(pk=json.pk)
    assert json_update.data_default == {"a": 2}

    await JSONFields.objects.filter(pk=json.pk).update(data_default=JsonSet(F("data_default"), "$.a", 3))
    json_update = await JSONFields.objects.get(pk=json.pk)
    assert json_update.data_default == {"a": 3}


@pytest.mark.asyncio
async def test_refresh_from_db(db):
    int_field = await IntFields.objects.create(intnum=1, intnum_null=2)
    int_field_in_db = await IntFields.objects.get(pk=int_field.pk)
    int_field_in_db.intnum = F("intnum") + 1
    await int_field_in_db.save(update_fields=["intnum"])
    assert int_field_in_db.intnum != 2
    assert int_field_in_db.intnum_null == 2

    await int_field_in_db.refresh_from_db(fields=["intnum"])
    assert int_field_in_db.intnum == 2
    assert int_field_in_db.intnum_null == 2

    int_field_in_db.intnum = F("intnum") + 1
    await int_field_in_db.save()
    assert int_field_in_db.intnum != 3
    assert int_field_in_db.intnum_null == 2

    await int_field_in_db.refresh_from_db()
    assert int_field_in_db.intnum == 3
    assert int_field_in_db.intnum_null == 2


@pytest.mark.asyncio
async def test_save_with_f_expression_stays_unresolved_and_applies_again_on_second_save(db):
    """The documented contract: an F() expression assigned before save() is evaluated by the
    database and left unresolved on the instance, so a second save() applies it a second time -
    refresh_from_db() is what reads the written value back and drops the expression."""
    obj = await IntFields.objects.create(intnum=10)
    obj.intnum = F("intnum") + 1

    await obj.save()
    await obj.save()
    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 12

    await obj.refresh_from_db(fields=["intnum"])
    assert obj.intnum == 12
    await obj.save()
    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 12


@requires_features(supports_update_limit_order_by=True)
@pytest.mark.asyncio
async def test_update_with_limit_ordering(db):
    await Tournament.objects.create(name="1")
    t2 = await Tournament.objects.create(name="1")
    await Tournament.objects.filter(name="1").order_by("-id").limit(1).update(name="2")
    assert (await Tournament.objects.get(pk=t2.pk)).name == "2"
    assert await Tournament.objects.filter(name="1").count() == 1


# hare.sql does not translate ** to POWER in MSSQL
@pytest.mark.asyncio
async def test_update_with_case_when_and_f(db):
    event1 = await IntFields.objects.create(intnum=1)
    event2 = await IntFields.objects.create(intnum=2)
    event3 = await IntFields.objects.create(intnum=3)
    await (
        IntFields.objects.all()
        .annotate(
            intnum_updated=Case(
                When(
                    Q(intnum=1),
                    then=F("intnum") + 1,
                ),
                When(
                    Q(intnum=2),
                    then=F("intnum") * 2,
                ),
                default=F("intnum") ** 3,
            )
        )
        .update(intnum=F("intnum_updated"))
    )

    for e in [event1, event2, event3]:
        await e.refresh_from_db()
    assert event1.intnum == 2
    assert event2.intnum == 4
    assert event3.intnum == 27


@pytest.mark.asyncio
async def test_update_with_function_annotation(db):
    tournament = await Tournament.objects.create(name="aaa")
    await (
        Tournament.objects.filter(pk=tournament.pk)
        .annotate(
            upped_name=Upper(F("name")),
        )
        .update(name=F("upped_name"))
    )
    assert (await Tournament.objects.get(pk=tournament.pk)).name == "AAA"


@pytest.mark.asyncio
async def test_update_with_f_expression_crossing_a_relation_raises_clear_error(db):
    """.update()'s own SET clause resolves each Expression via .get_result(), keeping only
    .term and silently discarding .joins - for a plain F("same_model_field") that's fine
    (joins is always empty), but F("relation__field") DOES populate .joins (the JOIN needed to
    reach the related table), and dropping it used to render a SET clause referencing a table
    the query never actually joins at all, crashing with a confusing "no such column"/"missing
    FROM-clause entry" instead of this clear, actionable error. Neither real backend's UPDATE
    supports a SET clause referencing a joined table without dialect-specific syntax this code
    doesn't generate (Postgres UPDATE...FROM, SQLite a correlated scalar subquery)."""
    tournament = await Tournament.objects.create(name="T1")
    event = await Event.objects.create(name="E1", tournament=tournament)

    with pytest.raises(QueryError, match="joined table"):
        await Event.objects.filter(event_id=event.event_id).update(alias=F("tournament__id"))


@pytest.mark.asyncio
async def test_update_with_filter_subquery(db):
    t1 = await Tournament.objects.create(name="1")
    r1 = await Reporter.objects.create(name="1")
    e1 = await Event.objects.create(name="1", tournament=t1, reporter=r1)

    # NOTE: this is intentionally written with Subquery and known PKs to test
    # whether subqueries are parameterized correctly.
    await Event.objects.filter(
        tournament_id__in=Subquery(Tournament.objects.filter(pk__in=[t1.pk]).values("id")),
        reporter_id__in=Subquery(Reporter.objects.filter(pk__in=[r1.pk]).values("id")),
    ).update(token="hello_world")

    await e1.refresh_from_db(fields=["token"])
    assert e1.token == "hello_world"


@pytest.mark.asyncio
async def test_update_with_set_value_subquery(db):
    """A Subquery(...) used as a .update() SET VALUE (not a filter value, see
    test_update_with_filter_subquery above) used to render unparenthesized - _set_sql() called
    value.get_sql(ctx) without ctx.copy(subquery=True), unlike _values_sql()/_from_sql(), so the
    nested SELECT landed bare after '=' (a syntax error on every dialect). Regression test: the
    correlated aggregate subquery must both execute and produce the right value."""
    author = await Author.objects.create(name="A1")
    other = await Author.objects.create(name="Other")
    b1 = await Book.objects.create(name="b1", author=author, rating=5.0)
    await Book.objects.create(name="b2", author=author, rating=1.0)
    await Book.objects.create(name="b3", author=other, rating=100.0)

    await Book.objects.filter(pk=b1.pk).update(
        rating=Subquery(
            Book.objects.filter(author_id=OuterReference("author_id"))
            .values("author_id")
            .annotate(total=Sum("rating"))
            .values("total")
        )
    )

    await b1.refresh_from_db(fields=["rating"])
    assert b1.rating == pytest.approx(6.0)


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_get_update_sql_materializes_update_fields_once(db):
    """get_update_sql()'s cache key must not consume update_fields as a side
    effect - it's typed Iterable[str], and a one-shot generator reused both
    for the cache key and the SET-clause loop would silently produce a SET
    clause with 0 fields the moment caching is involved."""
    await Tournament.objects.create(name="1")
    executor = InstanceWriter(Tournament, Tournament.get_connection())

    sql, _parameter_layout = executor.get_update_sql(iter(["name"]), None)
    assert "SET" in sql
    assert '"name"' in sql

    # Second call with an equivalent (but distinct) generator must hit the
    # same cache entry, proving the cache key itself doesn't rely on the
    # generator's identity or on it staying un-consumed.
    cached_sql, _parameter_layout = executor.get_update_sql(iter(["name"]), None)
    assert cached_sql == sql


# ============================================================================
# bulk_update() against a composite primary key
# ============================================================================


@pytest.mark.asyncio
async def test_bulk_update_composite_pk(db):
    a = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    b = await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    untouched = await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    a.name = "A2"
    b.name = "B2"
    rows_affected = await CompositePkThing.objects.bulk_update([a, b], fields=["name"])

    assert rows_affected == 2
    assert (await CompositePkThing.objects.get(thing_id=1, revision=1)).name == "A2"
    assert (await CompositePkThing.objects.get(thing_id=1, revision=2)).name == "B2"
    assert (await CompositePkThing.objects.get(thing_id=2, revision=1)).name == untouched.name == "C"


@pytest.mark.asyncio
async def test_bulk_update_composite_pk_three_columns(db):
    """Not hardcoded to exactly 2 pk columns."""
    x = await CompositePkTriple.objects.create(a=1, b=2, c=3, name="X")
    await CompositePkTriple.objects.create(a=1, b=2, c=4, name="Y")

    x.name = "X2"
    rows_affected = await CompositePkTriple.objects.bulk_update([x], fields=["name"])

    assert rows_affected == 1
    assert (await CompositePkTriple.objects.get(a=1, b=2, c=3)).name == "X2"
    assert (await CompositePkTriple.objects.get(a=1, b=2, c=4)).name == "Y"


@pytest.mark.asyncio
async def test_bulk_update_composite_pk_multiple_batches(db):
    """Same batching path as the existing single-pk test_bulk_update_multiple_batches, with a
    composite pk instead."""
    objs = [await CompositePkThing.objects.create(thing_id=i, revision=1, name=f"N{i}") for i in range(5)]
    for obj in objs:
        obj.name = f"{obj.name}-updated"

    rows_affected = await CompositePkThing.objects.bulk_update(objs, fields=["name"], batch_size=2)

    assert rows_affected == 5
    for i in range(5):
        assert (await CompositePkThing.objects.get(thing_id=i, revision=1)).name == f"N{i}-updated"


@pytest.mark.asyncio
async def test_bulk_update_incomplete_composite_pk_raises(db):
    """A composite pk's `.pk` is always a tuple, never bare None, even when one of its member
    fields wasn't set - the "has a primary key" guard must check inside the tuple, not just
    `obj.pk is None`, or an incomplete composite pk would silently build a WHERE ... = NULL clause
    that matches nothing instead of raising."""
    incomplete = CompositePkThing(thing_id=1, name="incomplete")  # revision left unset -> None

    with pytest.raises(ValueError, match="primary key"):
        await CompositePkThing.objects.bulk_update([incomplete], fields=["name"])


@pytest.mark.asyncio
async def test_bulk_update_no_pk_raises_regular_model(db):
    """Regression: the single-pk guard still works after being generalized for composite pk."""
    unsaved = Tournament(name="unsaved")

    with pytest.raises(ValueError, match="primary key"):
        await Tournament.objects.bulk_update([unsaved], fields=["name"])


@pytest.mark.asyncio
async def test_save_does_not_ask_for_the_primary_key_back(db):
    """Bug: save()'s UPDATE of a model with a database-generated primary key ended with
    RETURNING of that key - a column an UPDATE never generates anew - making the database return
    a row on every save(). Only the columns it computes again (a GeneratedField) come back."""
    tournament = await Tournament.objects.create(name="Spring")
    tournament.name = "Summer"
    async with capture_queries() as counter:
        await tournament.save(update_fields=["name"])

    (update_sql,) = [query for query in counter.queries if query.lstrip().upper().startswith("UPDATE")]
    assert "RETURNING" not in update_sql.upper()
    assert (await Tournament.objects.get(id=tournament.id)).name == "Summer"


@pytest.mark.asyncio
@pytest.mark.plan_verification_after_test
async def test_update_and_delete_reuse_the_where_clause_of_the_same_filter_shape(db):
    """update()/delete() over the base table's own columns resolve their filters once per shape -
    a second query of the same shape puts its own values into the stored WHERE clause and writes
    exactly its own rows."""
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    third = await Tournament.objects.create(name="third")
    await Tournament.objects.filter(id=first.id, name="first").update(desc="one")
    await Tournament.objects.filter(id=first.id, name="first").delete()

    with patch.object(QueryConditions, "get_filters", side_effect=AssertionError("filters resolved again")):
        assert await Tournament.objects.filter(id=second.id, name="second").update(desc="two") == 1
    with patch.object(QueryConditions, "get_filters", side_effect=AssertionError("filters resolved again")):
        assert await Tournament.objects.filter(id=third.id, name="third").delete() == 1

    assert await Tournament.objects.filter(id=first.id).exists() is False
    assert (await Tournament.objects.get(id=second.id)).desc == "two"
    assert await Tournament.objects.filter(id=third.id).exists() is False
    assert await Tournament.objects.filter(id=second.id, name="other").update(desc="never") == 0


@pytest.mark.asyncio
async def test_update_across_a_relation_resolves_its_filters_each_time(db):
    """A filter joining another table is never served from the stored WHERE clause - every call
    picks its rows by its own values."""
    spring = await Tournament.objects.create(name="spring")
    autumn = await Tournament.objects.create(name="autumn")
    spring_event = await Event.objects.create(name="a", tournament=spring)
    autumn_event = await Event.objects.create(name="b", tournament=autumn)

    await Event.objects.filter(tournament__name="spring").update(name="spring event")
    await Event.objects.filter(tournament__name="autumn").update(name="autumn event")

    assert (await Event.objects.get(pk=spring_event.pk)).name == "spring event"
    assert (await Event.objects.get(pk=autumn_event.pk)).name == "autumn event"
