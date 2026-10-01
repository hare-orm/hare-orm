from uuid import UUID, uuid4

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import (
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    QueryError,
    UnSupportedError,
)
from hare.fields.base import DatabaseDefault
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.query.expressions import F
from hare.query.statements.write.bulk_write_batches import BulkWriteBatches
from hare.sql.terms import LiteralValue
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    CallableDefault,
    CompositePkThing,
    DefaultModel,
    DirtyTrackedThing,
    Event,
    IntFields,
    Tag,
    Tournament,
    UniqueName,
    UpsertTarget,
    UUIDPkModel,
    VersionedUniqueAutoNow,
    Widget,
    WidgetTagMembership,
)


def assert_list_sort_equal(actual, expected, sorted_key="id"):
    """Assert two lists are equal after sorting by the given key."""
    assert sorted(actual, key=lambda x: x[sorted_key]) == sorted(expected, key=lambda x: x[sorted_key])


@pytest.mark.asyncio
async def test_bulk_create(db_truncate):
    """Test basic bulk create operation."""
    await UniqueName.objects.bulk_create([UniqueName() for _ in range(1000)])
    all_ = await UniqueName.objects.all().values("id", "name")
    inc = all_[0]["id"]
    assert_list_sort_equal(
        all_,
        [{"id": val + inc, "name": None} for val in range(1000)],
        sorted_key="id",
    )


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_update_fields(db_truncate):
    """Test bulk create with update_fields on conflict."""
    await UniqueName.objects.bulk_create([UniqueName(name="name")])
    await UniqueName.objects.bulk_create(
        [UniqueName(name="name", optional="optional")],
        update_fields=["optional"],
        on_conflict=["name"],
    )
    all_ = await UniqueName.objects.all().values("name", "optional")
    assert all_ == [{"name": "name", "optional": "optional"}]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_more_that_one_update_fields(db_truncate):
    """Test bulk create with multiple update_fields on conflict."""
    await UniqueName.objects.bulk_create([UniqueName(name="name")])
    await UniqueName.objects.bulk_create(
        [UniqueName(name="name", optional="optional", other_optional="other_optional")],
        update_fields=["optional", "other_optional"],
        on_conflict=["name"],
    )
    all_ = await UniqueName.objects.all().values("name", "optional", "other_optional")
    assert all_ == [
        {
            "name": "name",
            "optional": "optional",
            "other_optional": "other_optional",
        }
    ]


@pytest.mark.asyncio
async def test_bulk_create_with_batch_size(db_truncate):
    """Test bulk create with batch_size parameter."""
    await UniqueName.objects.bulk_create([UniqueName(id=id_ + 1) for id_ in range(1000)], batch_size=100)
    all_ = await UniqueName.objects.all().values("id", "name")
    assert_list_sort_equal(
        all_,
        [{"id": val + 1, "name": None} for val in range(1000)],
        sorted_key="id",
    )


@pytest.mark.asyncio
async def test_bulk_create_with_specified(db_truncate):
    """Test bulk create with specified IDs."""
    await UniqueName.objects.bulk_create([UniqueName(id=id_) for id_ in range(1000, 2000)])
    all_ = await UniqueName.objects.all().values("id", "name")
    assert_list_sort_equal(
        all_,
        [{"id": id_, "name": None} for id_ in range(1000, 2000)],
        sorted_key="id",
    )


@pytest.mark.asyncio
async def test_bulk_create_mix_specified(db_truncate):
    """Test bulk create with mix of specified and auto-generated IDs."""
    predefined_start = 40000
    predefined_end = 40150
    undefined_count = 100

    await UniqueName.objects.bulk_create(
        [UniqueName(id=id_) for id_ in range(predefined_start, predefined_end)]
        + [UniqueName() for _ in range(undefined_count)]
    )

    all_ = await UniqueName.objects.all().order_by("id").values("id", "name")
    predefined_count = predefined_end - predefined_start
    assert len(all_) == (predefined_count + undefined_count)

    if all_[0]["id"] == predefined_start:
        assert sorted(all_[:predefined_count], key=lambda x: x["id"]) == [
            {"id": id_, "name": None} for id_ in range(predefined_start, predefined_end)
        ]
        inc = all_[predefined_count]["id"]
        assert sorted(all_[predefined_count:], key=lambda x: x["id"]) == [
            {"id": val + inc, "name": None} for val in range(undefined_count)
        ]
    else:
        inc = all_[0]["id"]
        assert sorted(all_[:undefined_count], key=lambda x: x["id"]) == [
            {"id": val + inc, "name": None} for val in range(undefined_count)
        ]
        assert sorted(all_[undefined_count:], key=lambda x: x["id"]) == [
            {"id": id_, "name": None} for id_ in range(predefined_start, predefined_end)
        ]


@pytest.mark.asyncio
async def test_bulk_create_uuidpk(db_truncate):
    """Test bulk create with UUID primary key model."""
    await UUIDPkModel.objects.bulk_create([UUIDPkModel() for _ in range(1000)])
    res = await UUIDPkModel.objects.all().values_list("id", flat=True)
    assert len(res) == 1000
    assert isinstance(res[0], UUID)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_in_transaction(db_truncate):
    """Test bulk create inside transaction."""
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create([UniqueName() for _ in range(1000)])
    all_ = await UniqueName.objects.all().order_by("id").values("id", "name")
    inc = all_[0]["id"]
    assert all_ == [{"id": val + inc, "name": None} for val in range(1000)]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_uuidpk_in_transaction(db_truncate):
    """Test bulk create with UUID PK inside transaction."""
    async with Transactions.atomic():
        await UUIDPkModel.objects.bulk_create([UUIDPkModel() for _ in range(1000)])
    res = await UUIDPkModel.objects.all().values_list("id", flat=True)
    assert len(res) == 1000
    assert isinstance(res[0], UUID)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_fail(db_truncate):
    """Test bulk create fails with duplicate names."""
    with pytest.raises(IntegrityError):
        await UniqueName.objects.bulk_create(
            [UniqueName(name=str(i)) for i in range(10)] + [UniqueName(name=str(i)) for i in range(10)]
        )


@pytest.mark.asyncio
async def test_bulk_create_uuidpk_fail(db_truncate):
    """Test bulk create fails with duplicate UUID PKs."""
    val = uuid4()
    with pytest.raises(IntegrityError):
        await UUIDPkModel.objects.bulk_create([UUIDPkModel(id=val) for _ in range(10)])


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_in_transaction_fail(db_truncate):
    """Test bulk create fails inside transaction with duplicates."""
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create(
                [UniqueName(name=str(i)) for i in range(10)] + [UniqueName(name=str(i)) for i in range(10)]
            )


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_uuidpk_in_transaction_fail(db_truncate):
    """Test bulk create with UUID PK fails in transaction with duplicates."""
    val = uuid4()
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            await UUIDPkModel.objects.bulk_create([UUIDPkModel(id=val) for _ in range(10)])


@pytest.mark.asyncio
async def test_bulk_create_multi_column_values_land_in_correct_row_and_column(db_truncate):
    """_execute_parameterized_inserts (mutation.py) pre-binds every value's Parameter(idx=...)
    by hand instead of hare.sql's lazy per-value dispatch - a real risk for an off-by-one or
    row/column transposition bug in that hand-rolled indexing, which a single-column or
    always-None-valued bulk_create wouldn't catch. Distinct values in every column of every
    row, across multiple chunks (batch_size forces 4 of them), so a misindexed value would
    show up as a mismatched row.
    """
    objects = [UniqueName(name=f"name{i}", optional=f"opt{i}", other_optional=f"other{i}") for i in range(37)]
    await UniqueName.objects.bulk_create(objects, batch_size=10)
    rows = await UniqueName.objects.all().order_by("name").values("name", "optional", "other_optional")
    expected = sorted(
        ({"name": f"name{i}", "optional": f"opt{i}", "other_optional": f"other{i}"} for i in range(37)),
        key=lambda r: r["name"],
    )
    assert rows == expected


@pytest.mark.asyncio
async def test_bulk_create_multi_row_insert_uses_one_placeholder_term_per_row(db_truncate, monkeypatch):
    """_execute_via_multi_row_statement (mutation.py) used to allocate one Parameter(idx=...)
    object PER SCALAR VALUE (rows x columns per chunk) to build each row's placeholder text -
    replaced with one LiteralValue-wrapped placeholder string per ROW instead. Locks in the
    allocation-count fix: for N rows x M columns, exactly N LiteralValue instances must be
    constructed by this path, not N x M."""
    db = UniqueName.get_connection()
    if not db.features.execute_many_scales_poorly:
        pytest.skip(
            "this driver's plain bulk_create() uses _execute_via_execute_many, not the "
            "multi-row-statement path this test targets"
        )

    construction_count = 0
    original_init = LiteralValue.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal construction_count
        construction_count += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(LiteralValue, "__init__", counting_init)
    objects = [UniqueName(name=f"count{i}", optional=f"opt{i}", other_optional=f"other{i}") for i in range(5)]
    await UniqueName.objects.bulk_create(objects)

    assert construction_count == 5  # one per row, not one per (row, column) = 15


@pytest.mark.asyncio
async def test_bulk_create_multi_row_insert_sql_has_one_placeholder_group_per_row(db_truncate):
    """The SQL actually sent to the driver for a multi-row insert must still be the same shape
    the old per-value Parameter() approach produced: one comma-separated placeholder group per
    row, in row-major order - "($1,$2,$3),($4,$5,$6)" for 2 rows of 3 columns, not merged/
    malformed by the LiteralValue-wrapped placeholder text replacing individual Parameter terms."""
    db = UniqueName.get_connection()
    if not db.features.execute_many_scales_poorly:
        pytest.skip(
            "this driver's plain bulk_create() uses _execute_via_execute_many, not the "
            "multi-row-statement path this test targets"
        )

    captured_sql = []

    def capture(event):
        sql = event.sql
        captured_sql.append(sql)

    Observers.observe(QueryExecuted, capture)
    try:
        await UniqueName.objects.bulk_create(
            [
                UniqueName(name="sql-a", optional="opt-a", other_optional="other-a"),
                UniqueName(name="sql-b", optional="opt-b", other_optional="other-b"),
            ]
        )
        # run_query_hooks() fires hooks in the background (never blocks the query itself) -
        # drain pending hooks so `capture` has actually run before asserting on captured_sql.
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, capture)

    insert_sql = next(sql for sql in captured_sql if sql and sql.strip().upper().startswith("INSERT"))
    assert "VALUES ($1,$2,$3),($4,$5,$6)" in insert_sql


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts(db_truncate):
    """Test bulk create with ignore_conflicts option."""
    name1 = UniqueName(name="name1")
    name2 = UniqueName(name="name2")
    await UniqueName.objects.bulk_create([name1, name2])
    await UniqueName.objects.bulk_create([name1, name2], ignore_conflicts=True)
    with pytest.raises(IntegrityError):
        await UniqueName.objects.bulk_create([name1, name2])


@pytest.mark.asyncio
async def test_bulk_create_update_fields_rejects_generated_field(db):
    """ON CONFLICT DO UPDATE builds its own SET clause directly (BulkCreateQuery.
    _apply_on_conflict()), with no equivalent to .update()'s/bulk_update()'s own GeneratedField
    guard - a caller could target one in update_fields, reaching the database and surfacing a
    raw, dialect-leaking driver error instead of a clean ConfigurationError raised up front."""
    widget = await Widget.objects.create(name="W")
    tag = await Tag.objects.create(name="T")
    with pytest.raises(QueryError, match="generated field"):
        await WidgetTagMembership.objects.bulk_create(
            [WidgetTagMembership(widget=widget, tag=tag, weight=5)],
            on_conflict=("id",),
            update_fields=["weight_doubled"],
        )


@pytest.mark.asyncio
async def test_bulk_update_multi_field_values_land_in_correct_row_and_column(db_truncate):
    """BulkUpdateQuery._make_queries (mutation.py) pre-binds each VALUES-row value's
    Parameter(idx=...) by hand (pk column(s), then each updated field) instead of hare.sql's
    lazy per-value dispatch - the same off-by-one/transposition risk as bulk_create's
    equivalent fix, here across pk+multiple updated fields per row rather than just fields.
    Distinct values in both updated columns of every row, across multiple chunks (batch_size
    forces 4 of them), so a misindexed value would show up as a mismatched row.
    """
    objects = [await IntFields.objects.create(intnum=0, intnum_null=0) for _ in range(37)]
    for i, obj in enumerate(objects):
        obj.intnum = i * 10
        obj.intnum_null = i * 100
    await IntFields.objects.bulk_update(objects, fields=["intnum", "intnum_null"], batch_size=10)

    rows = await IntFields.objects.all().order_by("id").values("id", "intnum", "intnum_null")
    expected = [{"id": obj.id, "intnum": i * 10, "intnum_null": i * 100} for i, obj in enumerate(objects)]
    assert rows == expected


@pytest.mark.asyncio
async def test_bulk_create_resolves_async_default_callables(db_truncate):
    """Model.save() always calls self._set_async_default_field() first, resolving any field
    whose default= is a coroutine function and is still pending in self._await_when_save -
    bulk_create() used to skip straight to serialization instead, which does a literal
    attribute read on a field that was never actually assigned, crashing with a raw
    AttributeError instead of writing the resolved default."""
    obj = CallableDefault()
    await CallableDefault.objects.bulk_create([obj])

    # bulk_create() never populates generated PKs back onto the passed-in objects (see its own
    # docstring), so obj.pk isn't usable to look the row back up - fetch the lone inserted row
    # instead.
    refreshed = await CallableDefault.objects.get()
    assert refreshed.callable_default == "callable_default"
    assert refreshed.async_default == "async_callable_default"


@pytest.mark.asyncio
async def test_bulk_update_resolves_async_default_callables(db_truncate):
    """Same gap as test_bulk_create_resolves_async_default_callables, but for bulk_update():
    BulkUpdateQuery.__await__ used to skip straight to _make_queries()'s serialize_instances()
    call, which does a literal attribute read on a field that was never actually assigned - a
    field whose async default= callable is still pending in self._await_when_save, e.g. an
    object built directly with an explicit pk and never saved (bulk_update()'s own contract is
    "objects I already hold by PK", not necessarily objects that went through save()) -
    crashing with a raw AttributeError instead of writing the resolved default."""
    await CallableDefault.objects.create(id=1)

    forged = CallableDefault(id=1)
    await CallableDefault.objects.bulk_update([forged], fields=["async_default"])

    refreshed = await CallableDefault.objects.get(pk=1)
    assert refreshed.async_default == "async_callable_default"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_populates_real_ids(db_truncate):
    """returning=True must populate the real, DB-generated pk onto every passed-in object -
    unlike the default (returning=False) contract, which leaves it unset. Spans multiple
    chunks (batch_size forces several separate multi-row INSERT ... RETURNING statements), so a
    row-order mixup across chunks would show up as a wrong/duplicate id.
    """
    objects = [UniqueName(name=f"rp{i}") for i in range(37)]
    await UniqueName.objects.bulk_create(objects, batch_size=10, returning=True)

    assert all(obj.id is not None for obj in objects)
    assert len({obj.id for obj in objects}) == len(objects)

    for obj in objects:
        refreshed = await UniqueName.objects.get(id=obj.id)
        assert refreshed.name == obj.name


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_leaves_explicit_pk_objects_unchanged(db_truncate):
    """A model whose pk is resolved client-side before INSERT (e.g. UUIDField(primary_key=True)'s
    own default=uuid4) is never part of the RETURNING-backfilled group - returning=True must
    not disturb the pk it was already given."""
    objects = [UUIDPkModel() for _ in range(5)]
    original_ids = [obj.id for obj in objects]

    await UUIDPkModel.objects.bulk_create(objects, returning=True)

    assert [obj.id for obj in objects] == original_ids
    res = await UUIDPkModel.objects.all().values_list("id", flat=True)
    assert set(res) == set(original_ids)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_with_composite_pk_model(db_truncate):
    """A composite pk is always resolved client-side (never DB-generated - see CompositePkThing's
    own model docs elsewhere), so returning=True has nothing to backfill for it - must be a
    no-op, not a crash."""
    objects = [CompositePkThing(thing_id=1, revision=i, name=f"r{i}") for i in range(3)]
    await CompositePkThing.objects.bulk_create(objects, returning=True)

    rows = await CompositePkThing.objects.all().order_by("revision").values("thing_id", "revision", "name")
    assert rows == [{"thing_id": 1, "revision": i, "name": f"r{i}"} for i in range(3)]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_mixed_with_explicit_pk_objects(db_truncate):
    """A single bulk_create() call mixing auto-generated and explicitly-set pks (mirrors
    test_bulk_create_mix_specified) - only the auto-generated group is backfilled by
    returning, the explicit ids must survive untouched."""
    explicit = [UniqueName(id=90000 + i) for i in range(5)]
    auto = [UniqueName(name=f"auto{i}") for i in range(5)]

    await UniqueName.objects.bulk_create(explicit + auto, returning=True)

    assert [obj.id for obj in explicit] == [90000 + i for i in range(5)]
    assert all(obj.id is not None and obj.id not in {e.id for e in explicit} for obj in auto)
    assert len({obj.id for obj in auto}) == len(auto)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_ids_usable_for_downstream_fk_relations(db_truncate):
    """The real ids populated by returning=True must be immediately usable to build
    relations off of - not just cosmetically "look real"."""
    tournaments = [Tournament(name=f"t{i}") for i in range(3)]
    await Tournament.objects.bulk_create(tournaments, returning=True)
    assert all(t.id is not None for t in tournaments)
    assert len({t.id for t in tournaments}) == 3

    # tournament_id=... (the shadow FK column) rather than tournament=tournaments[i] - the FK
    # object setter requires its target to already be marked _saved_in_db, which bulk_create()
    # never does for the objects it inserts (it has no per-object save() call to hang that on),
    # unrelated to whether returning populated a real id.
    events = [Event(event_id=90000 + i, name=f"e{i}", tournament_id=tournaments[i].id) for i in range(3)]
    await Event.objects.bulk_create(events)

    for i, tournament in enumerate(tournaments):
        fetched = await Event.objects.get(event_id=90000 + i)
        assert fetched.tournament_id == tournament.id


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_populates_db_default_fields(db_truncate):
    """returning=True now brings back every db_default column that was omitted from the
    INSERT (every instance relied on its DatabaseDefault sentinel), not just the pk - matching
    what a plain create()/save() already does. DefaultModel has ONLY db_default fields besides
    its auto pk, so this exercises the DEFAULT VALUES fallback path (_build_default_values_sql())
    specifically - the ordinary multi-row VALUES path never reaches for zero-column objects."""
    objects = [DefaultModel(), DefaultModel()]
    await DefaultModel.objects.bulk_create(objects, returning=True)

    assert all(obj.id is not None for obj in objects)
    for obj in objects:
        assert obj.int_default == 1
        assert obj.float_default == 1.5
        assert obj.bool_default is True
        assert obj.char_default == "hare"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_false_leaves_db_default_fields_untouched(db_truncate):
    """The default (returning=False) contract must stay exactly as before - no column,
    pk or db_default alike, gets populated back onto the passed-in objects."""
    objects = [DefaultModel(), DefaultModel()]
    await DefaultModel.objects.bulk_create(objects)

    assert all(obj.id is None for obj in objects)
    from hare.fields.base import DatabaseDefault

    assert all(isinstance(obj.int_default, DatabaseDefault) for obj in objects)


@pytest.mark.asyncio
async def test_bulk_update_rejects_unset_db_default_field(db_truncate):
    """execute_update() (Model.save()) silently omits a field from its SET clause when the
    instance still holds its DatabaseDefault sentinel, leaving the DB's own default untouched -
    bulk_update() can't do the same per-object (every object in a chunk shares one fixed SET
    column list), so it used to crash deep inside serialize_instances() with a confusing
    "int() argument must be ... not 'DatabaseDefault'" ValidationError instead. Must now reject
    clearly, up front, naming the actual field."""
    objects = [DefaultModel(id=1), DefaultModel(id=2)]
    assert all(isinstance(obj.int_default, DatabaseDefault) for obj in objects)

    with pytest.raises(QueryError, match="int_default"):
        await DefaultModel.objects.bulk_update(objects, fields=["int_default"])


@pytest.mark.asyncio
async def test_bulk_update_rejects_partial_object_missing_auto_now_field(db_truncate):
    """An auto_now field is read+bumped on every bulk_update() regardless of which OTHER fields
    are in fields= - a caller never lists it themselves. A .only(...)-partial object missing it
    used to crash with a raw AttributeError deep inside serialize_instances() instead of this
    clear message, mirroring save()'s own identical guard for the same situation."""
    obj = await VersionedUniqueAutoNow.objects.create(name="a", tag="t-auto-now-partial")
    partial = await VersionedUniqueAutoNow.objects.filter(id=obj.id).only("id", "name", "version").get()
    partial.name = "b"

    with pytest.raises(IncompleteInstanceError, match="updated_at"):
        await VersionedUniqueAutoNow.objects.bulk_update([partial], fields=["name"])


@pytest.mark.asyncio
async def test_bulk_update_rejects_partial_object_missing_optimistic_lock_field(db_truncate):
    """Meta.optimistic_lock_field is read on every bulk_update() (the staleness-check WHERE clause)
    regardless of which OTHER fields are in fields=. A .only(...)-partial object missing it used
    to crash with a raw AttributeError deep inside _pk_and_version_db_values() instead of this
    clear message, mirroring save()'s own identical guard for the same situation."""
    obj = await VersionedUniqueAutoNow.objects.create(name="a", tag="t-version-partial")
    partial = await VersionedUniqueAutoNow.objects.filter(id=obj.id).only("id", "name", "updated_at").get()
    partial.name = "b"

    with pytest.raises(IncompleteInstanceError, match="version"):
        await VersionedUniqueAutoNow.objects.bulk_update([partial], fields=["name"])


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_update_savepoint_rollback_for_an_unrelated_reason_rolls_back_version_and_auto_now(db_truncate):
    """bulk_update() bumps optimistic_lock_field/an auto_now field optimistically before the write's own
    outcome is known, same as save() - but unlike save() (fixed in 3306b521/afc2ba98), it never
    registered either bump to be undone if the enclosing transaction/savepoint later rolls back
    for an unrelated reason. Confirmed live: the in-memory bump survived the rollback while the
    DB row itself correctly reverted, desyncing this instance from the database with nothing else
    to catch it - the next, otherwise legitimate save() on the same instance would spuriously
    raise StaleObjectError as if a concurrent writer had touched the row, when none ever did."""
    thing = await VersionedUniqueAutoNow.objects.create(name="a", tag="t-bulk-rollback-restore")
    old_version = thing.version
    old_updated_at = thing.updated_at

    class UnrelatedFailure(Exception):
        pass

    with pytest.raises(UnrelatedFailure):
        async with Transactions.atomic():
            thing.name = "b"
            await VersionedUniqueAutoNow.objects.filter(pk=thing.pk).bulk_update([thing], fields=["name"])
            raise UnrelatedFailure

    fresh = await VersionedUniqueAutoNow.objects.get(pk=thing.pk)
    assert fresh.version == old_version
    assert fresh.updated_at == old_updated_at
    assert thing.version == old_version
    assert thing.updated_at == old_updated_at


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_bulk_create_returning_unsupported_on_sqlite(db_truncate):
    """SQLite's own RETURNING output order for a multi-row INSERT is explicitly unspecified by
    its documentation, so returned rows can't be reliably matched back to their source objects -
    returning=True must fail loudly instead of silently mismatching."""
    with pytest.raises(UnSupportedError):
        await UniqueName.objects.bulk_create([UniqueName(name="x")], returning=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_returning_matches_survivors_by_conflict_target(db_truncate):
    """ON CONFLICT DO NOTHING skips whichever rows collide, so RETURNING comes back shorter than
    objects_item and is no longer aligned with it positionally - only the surviving object (whose
    pk is DB-generated, unknown before the query runs) must get its id backfilled; the skipped
    one, which already exists in the database under a different pk, must be left untouched
    rather than mistakenly stamped with the survivor's id."""
    existing = await UniqueName.objects.create(name="dup")

    conflicting = UniqueName(name="dup")
    inserted = UniqueName(name="fresh")
    await UniqueName.objects.bulk_create(
        [conflicting, inserted],
        ignore_conflicts=True,
        on_conflict=["name"],
        returning=True,
    )

    assert conflicting.id is None
    assert inserted.id is not None
    assert inserted.id != existing.id


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_returning_without_on_conflict_raises_before_any_sql(db_truncate):
    """Without on_conflict=[...] naming the conflict target columns, a skipped row can't be told
    apart from a survivor once ON CONFLICT DO NOTHING actually skips at least one row - matching
    positionally would silently stamp the wrong pk onto the wrong object, so this must raise
    instead. It used to raise only AFTER the INSERT had already run (and only when a conflict
    really happened), leaving the surviving row in the table although the call failed - the
    combination is now rejected up front, whether or not any row would conflict."""
    await UniqueName.objects.create(name="dup")

    with pytest.raises(QueryError, match="on_conflict"):
        UniqueName.objects.bulk_create(
            [UniqueName(name="dup"), UniqueName(name="fresh2")],
            ignore_conflicts=True,
            returning=True,
        )
    assert not await UniqueName.objects.filter(name="fresh2").exists()

    with pytest.raises(QueryError, match="on_conflict"):
        UniqueName.objects.bulk_create([UniqueName(name="no-conflict")], ignore_conflicts=True, returning=True)
    with pytest.raises(QueryError, match="on_conflict"):
        UniqueName.objects.bulk_create(
            [UniqueName(name="no-conflict")],
            ignore_conflicts=True,
            on_conflict_constraint="uniquename_name_key",
            returning=True,
        )
    assert not await UniqueName.objects.filter(name="no-conflict").exists()


@pytest.mark.asyncio
async def test_bulk_update_rejects_expression_values_with_a_clear_error(db_truncate):
    """bulk_update() binds each object's value as a plain parameter of one shared statement, so an
    F() expression reaching to_db_value() used to fail with a confusing "int() argument must be
    ... not 'CombinedExpression'" ValidationError."""
    obj = await IntFields.objects.create(intnum=1)
    obj.intnum = F("intnum") + 10

    with pytest.raises(QueryError, match=r"F\(\)/expression.*update\(\)"):
        await IntFields.objects.bulk_update([obj], fields=["intnum"])

    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 1


@pytest.mark.asyncio
async def test_bulk_create_rejects_non_positive_batch_size_before_any_sql(db_truncate):
    for invalid_batch_size in (0, -1):
        with pytest.raises(QueryError, match="batch_size"):
            UniqueName.objects.bulk_create([UniqueName(name="x")], batch_size=invalid_batch_size)
    assert not await UniqueName.objects.filter(name="x").exists()


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_failing_on_a_later_batch_leaves_no_earlier_batch_behind(db_truncate):
    """Each batch is its own statement - without a transaction around them, a batch after the
    first hitting a unique violation left every earlier batch's rows in the table even though the
    call raised, so the caller couldn't tell how far it got."""
    await UniqueName.objects.create(name="dup")

    with pytest.raises(IntegrityError):
        await UniqueName.objects.bulk_create([UniqueName(name="first"), UniqueName(name="dup")], batch_size=1)

    assert not await UniqueName.objects.filter(name="first").exists()
    assert await UniqueName.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_failing_on_a_later_batch_inside_a_transaction_only_undoes_itself(db_truncate):
    """Inside a caller's own transaction the failed batches roll back to a savepoint, so the
    transaction stays usable and keeps the caller's earlier writes."""
    await UniqueName.objects.create(name="dup")

    async with Transactions.atomic():
        await UniqueName.objects.create(name="kept")
        with pytest.raises(IntegrityError):
            await UniqueName.objects.bulk_create([UniqueName(name="first"), UniqueName(name="dup")], batch_size=1)
        assert not await UniqueName.objects.filter(name="first").exists()
        assert await UniqueName.objects.filter(name="kept").exists()

    assert await UniqueName.objects.filter(name="kept").exists()


@pytest.mark.asyncio
async def test_bulk_create_returning_and_use_copy_together_raises(db_truncate):
    """The Postgres COPY protocol has no RETURNING support at all - the two strategies are
    mutually exclusive, checked eagerly (before either ever reaches the database)."""
    with pytest.raises(UnSupportedError, match="mutually exclusive"):
        UniqueName.objects.bulk_create([UniqueName(name="x")], returning=True, use_copy=True)


@pytest.mark.asyncio
async def test_bulk_create_use_copy_and_on_conflict_together_raises(db_truncate):
    """COPY performs a pure bulk insert with no ON CONFLICT support - any conflict-handling
    parameter combined with use_copy must fail eagerly rather than silently ignoring it."""
    with pytest.raises(UnSupportedError, match="ON CONFLICT"):
        UniqueName.objects.bulk_create(
            [UniqueName(name="x")],
            use_copy=True,
            update_fields=["optional"],
            on_conflict=["name"],
        )
    with pytest.raises(UnSupportedError, match="ON CONFLICT"):
        UniqueName.objects.bulk_create([UniqueName(name="x")], use_copy=True, ignore_conflicts=True)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_raises_unsupported_on_sqlite(db_truncate):
    """COPY is a Postgres wire-protocol concept with no SQLite equivalent."""
    with pytest.raises(UnSupportedError):
        await UniqueName.objects.bulk_create([UniqueName(name="x")], use_copy=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_inside_transaction_raises_on_rust_pg(db_truncate):
    """rust.pg's COPY support has no transaction-participating variant (copy_in is only
    exposed on pg.Client, not pg.Transaction) - unlike asyncpg, where copy is inherited
    unchanged by its own transaction client and genuinely participates via acquire_connection().
    Without an explicit guard, a COPY issued from inside a rust_pg transaction would silently
    run (and commit) on a separate pool connection instead of the transaction's own - the
    inserted rows would survive an outer rollback. Must fail loudly instead of doing that
    quietly. asyncpg has no such gap, so this only applies to rust_pg."""
    db = Tournament.get_connection()
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db).__module__:
        pytest.skip("rust_pg-specific transaction/COPY participation gap")
    with pytest.raises(UnSupportedError):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create([UniqueName(name="tx-copy")], use_copy=True)
    assert await UniqueName.objects.filter(name="tx-copy").count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_inside_transaction_participates_on_asyncpg(db_truncate):
    """asyncpg's transaction wrapper inherits copy unchanged, which reads the client's own
    `schema` to qualify the COPY target - the wrapper never copied it over, so any COPY issued
    inside a transaction died with AttributeError instead of joining it."""
    db = Tournament.get_connection()
    if "hare.dialects.postgresql.drivers.asyncpg" not in type(db).__module__:
        pytest.skip("asyncpg-specific transaction/COPY participation")

    class RollBack(Exception):
        pass

    with pytest.raises(RollBack):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create([UniqueName(name="tx-copy-rolled-back")], use_copy=True)
            raise RollBack()
    assert await UniqueName.objects.filter(name="tx-copy-rolled-back").count() == 0

    async with Transactions.atomic():
        await UniqueName.objects.bulk_create([UniqueName(name="tx-copy-kept")], use_copy=True)
    assert await UniqueName.objects.filter(name="tx-copy-kept").count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_matches_regular_insert_result(db_truncate):
    """use_copy=True must land byte-identical rows to the ordinary multi-row INSERT path - COPY
    is purely a faster wire transport for the same data, not a different write semantic."""
    regular_objects = [UniqueName(name=f"reg{i}", optional=f"opt{i}", other_optional=f"oo{i}") for i in range(25)]
    copy_objects = [UniqueName(name=f"cpy{i}", optional=f"opt{i}", other_optional=f"oo{i}") for i in range(25)]

    await UniqueName.objects.bulk_create(regular_objects)
    await UniqueName.objects.bulk_create(copy_objects, use_copy=True, batch_size=7)

    regular_rows = sorted(
        await UniqueName.objects.filter(name__startswith="reg").values("optional", "other_optional"),
        key=lambda r: r["optional"],
    )
    copy_rows = sorted(
        await UniqueName.objects.filter(name__startswith="cpy").values("optional", "other_optional"),
        key=lambda r: r["optional"],
    )
    assert len(copy_rows) == 25
    assert copy_rows == regular_rows


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_leaves_auto_pk_unset(db_truncate):
    """use_copy=True has no RETURNING support (enforced separately), so - like the default
    bulk_create() contract - it never populates a DB-generated pk back onto the caller's
    objects."""
    objects = [UniqueName(name=f"x{i}") for i in range(5)]
    await UniqueName.objects.bulk_create(objects, use_copy=True)
    assert all(obj.id is None for obj in objects)
    assert await UniqueName.objects.all().count() == 5


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_with_composite_pk_model(db_truncate):
    """A composite pk is always resolved client-side (see CompositePkThing's own model docs) -
    COPY must carry those explicit values through correctly, same as the regular INSERT path."""
    objects = [CompositePkThing(thing_id=1, revision=i, name=f"r{i}") for i in range(5)]
    await CompositePkThing.objects.bulk_create(objects, use_copy=True)

    rows = await CompositePkThing.objects.all().order_by("revision").values("thing_id", "revision", "name")
    assert rows == [{"thing_id": 1, "revision": i, "name": f"r{i}"} for i in range(5)]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_use_copy_mixed_with_explicit_and_auto_pk_objects(db_truncate):
    """Mirrors test_bulk_create_returning_mixed_with_explicit_pk_objects for use_copy - the
    explicit-pk and auto-pk groups go through two SEPARATE COPY calls (different column lists),
    both must land correctly in one bulk_create() call."""
    explicit = [UniqueName(id=95000 + i, name=f"exp{i}") for i in range(5)]
    auto = [UniqueName(name=f"auto{i}") for i in range(5)]
    if "rust_pg" in type(UniqueName._meta.db).__module__:
        # rust_pg's COPY can't join a transaction, so the two separate COPY calls can't be kept
        # all-or-nothing - refused before anything is written.
        with pytest.raises(UnSupportedError, match="explicit primary key"):
            await UniqueName.objects.bulk_create(explicit + auto, use_copy=True)
        assert await UniqueName.objects.all().count() == 0
        return

    await UniqueName.objects.bulk_create(explicit + auto, use_copy=True)

    explicit_rows = await UniqueName.objects.filter(id__in=[95000 + i for i in range(5)]).values("id", "name")
    assert sorted(explicit_rows, key=lambda r: r["id"]) == [{"id": 95000 + i, "name": f"exp{i}"} for i in range(5)]
    assert await UniqueName.objects.filter(name__startswith="auto").count() == 5


@pytest.mark.asyncio
async def test_bulk_create_marks_objects_saved_in_db(db_truncate):
    """bulk_create() used to never set _saved_in_db=True on its own objects, unlike Model.save()
    - every relation manager's own "has this instance been saved" gate (ReverseRelation._query/
    .create(), ManyToManyRelation.add()/remove(), all in fields/relational.py) then rejected a
    bulk-created object with "You should first call .save() on <Model>" even though its row was
    genuinely already in the DB with a real, known pk. Confirmed live before this fix on all 3
    backends."""
    tournaments = [Tournament(id=9001, name="A"), Tournament(id=9002, name="B")]
    await Tournament.objects.bulk_create(tournaments)
    assert all(t._saved_in_db for t in tournaments)

    widgets = [Widget(id=9001, name="w1")]
    await Widget.objects.bulk_create(widgets)
    widget = widgets[0]
    assert widget._saved_in_db

    tag = await Tag.objects.create(name="a-tag")
    await widget.tags.add(tag)
    assert await WidgetTagMembership.objects.filter(widget_id=widget.id, tag_id=tag.id).exists()

    tournament = tournaments[0]
    event = Event(event_id=9001, name="ev1", tournament_id=tournament.id)
    await Event.objects.bulk_create([event])
    assert event._saved_in_db
    events_via_backward = await tournament.events.all()
    assert [e.event_id for e in events_via_backward] == [9001]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_only_marks_survivors_saved_in_db(db_truncate):
    """ON CONFLICT DO NOTHING can silently skip inserting some objects' rows - unlike the plain
    bulk_create() case above, marking every object _saved_in_db=True unconditionally here would
    let a caller call .add()/.remove() or read a relation off an object whose row never actually
    landed. Only the object _populate_returned_fields_from_returning_rows() actually matches to
    a surviving RETURNING row gets marked."""
    await UniqueName.objects.create(name="dup")

    colliding = UniqueName(name="dup")
    surviving = UniqueName(name="fresh")
    await UniqueName.objects.bulk_create(
        [colliding, surviving],
        ignore_conflicts=True,
        on_conflict=["name"],
        returning=True,
    )

    assert colliding._saved_in_db is False
    assert surviving._saved_in_db is True


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_without_returning_marks_nobody_saved_in_db(db_truncate):
    """Without returning=True, there's no universally-available way to tell which objects
    survived ON CONFLICT DO NOTHING (mirrors the identical reasoning already established for
    dirty-tracking in BulkCreateQuery._run()) - leaving every object unmarked is the safer
    failure mode, even for one that didn't actually collide."""
    await UniqueName.objects.create(name="dup")

    colliding = UniqueName(name="dup")
    surviving = UniqueName(name="fresh2")
    await UniqueName.objects.bulk_create([colliding, surviving], ignore_conflicts=True)

    assert colliding._saved_in_db is False
    assert surviving._saved_in_db is False


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_rollback_restores_saved_in_db_and_auto_now_add(db_truncate):
    """bulk_create()'s own post-insert bookkeeping (_saved_in_db=True, auto_now/auto_now_add
    side-effects from to_db_value()) was never undone if the enclosing transaction/savepoint
    later rolled back for an unrelated reason - unlike save()'s equivalent path, which always
    registers a rollback-restore for exactly this. _saved_in_db stayed True with no row actually
    in the DB, and worse, auto_now_add (only recomputed while the current value is still None)
    permanently kept the failed attempt's own timestamp on every later retry of the same
    instance."""
    import datetime

    from tests.testmodels import DatetimeFields

    class UnrelatedFailure(Exception):
        pass

    obj = DatetimeFields(datetime=datetime.datetime(2020, 1, 1))
    with pytest.raises(UnrelatedFailure):
        async with Transactions.atomic():
            await DatetimeFields.objects.bulk_create([obj])
            raise UnrelatedFailure

    assert obj._saved_in_db is False
    # datetime_add's own type stub says datetime.datetime (never None) since auto_now_add fields
    # are always populated - true once saved, but this object's insert was rolled back, so it's
    # genuinely still None at runtime.
    assert obj.datetime_add is None

    await DatetimeFields.objects.bulk_create([obj])  # type: ignore[unreachable]
    assert obj._saved_in_db is True
    assert obj.datetime_add is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_rollback_restores_pk(db_truncate):
    """bulk_create(returning=True) assigns each object's real, DB-generated pk from RETURNING -
    unlike save() (which restores pk on rollback), the pk stayed permanently assigned to an
    object whose row never actually existed, once the enclosing transaction rolled back."""

    class UnrelatedFailure(Exception):
        pass

    objects = [UniqueName(name=f"rb{i}") for i in range(3)]
    with pytest.raises(UnrelatedFailure):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create(objects, returning=True)
            assert all(obj.id is not None for obj in objects)
            raise UnrelatedFailure

    assert all(obj.id is None for obj in objects)
    assert all(obj._saved_in_db is False for obj in objects)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_create_rollback_restores_the_dirty_tracking_baseline(db_truncate):
    """bulk_create() re-baselines each object's dirty-tracking snapshot right after INSERT - a
    later rollback left every field looking "clean" for an object with no row in the DB at all
    (get_dirty_fields() == {}), so a check like `if obj.get_dirty_fields(): save()` skipped a
    write that was genuinely still needed. Model.save() restores this on rollback already."""

    class UnrelatedFailure(Exception):
        pass

    obj = DirtyTrackedThing(name="fresh")
    dirty_before = obj.get_dirty_fields()
    assert dirty_before

    with pytest.raises(UnrelatedFailure):
        async with Transactions.atomic():
            await DirtyTrackedThing.objects.bulk_create([obj])
            assert obj.get_dirty_fields() == {}
            raise UnrelatedFailure

    assert obj.get_dirty_fields() == dirty_before


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_update_rollback_restores_the_dirty_tracking_baseline(db_truncate):
    """Same gap as bulk_create()'s: bulk_update() syncs the written fields into each object's
    dirty snapshot right after UPDATE, and a later rollback left them looking clean although the
    DB still holds the old value."""

    class UnrelatedFailure(Exception):
        pass

    obj = await DirtyTrackedThing.objects.create(name="before")
    obj.name = "after"
    assert "name" in obj.get_dirty_fields()

    with pytest.raises(UnrelatedFailure):
        async with Transactions.atomic():
            await DirtyTrackedThing.objects.bulk_update([obj], fields=["name"])
            assert "name" not in obj.get_dirty_fields()
            raise UnrelatedFailure

    assert "name" in obj.get_dirty_fields()
    assert (await DirtyTrackedThing.objects.get(pk=obj.pk)).name == "before"


def test_bind_param_safe_batch_size_uses_the_real_backend_limit_not_a_fixed_constant():
    """Regression test for the old MAX_BIND_PARAMS=65535 module constant: the real Postgres wire
    protocol limit is 32767 (the extended-query Bind message's parameter count is a signed
    int16) - exactly half of 65535, an int16-vs-uint16 mixup - and sqlite's real
    SQLITE_LIMIT_VARIABLE_NUMBER is a separate, similarly-sized but distinct number (32766 in a
    modern build, 999 pre-3.32.0). _bind_param_safe_batch_size() must derive its answer from
    whatever max_bind_params the caller passes (Capabilities.max_bind_params for the real
    backend in play), not a hardcoded figure that silently assumed 65535 for every dialect.
    """
    # 3 bind-parameterized columns per row (matches the live asyncpg bug repro: name/optional/
    # other_optional on UniqueName, id auto-generated). The real asyncpg limit (32767) yields a
    # smaller batch than the old wrong constant (65535) would have: 32767 // 3 = 10922 rows/chunk
    # (32766 bind params, safely under the real limit) versus the old, wrong 65535 // 3 = 21845
    # rows/chunk (65535 bind params - already twice the real limit on its own).
    assert BulkWriteBatches.get_bind_param_safe_batch_size(None, num_columns=3, max_bind_params=32767) == 10922
    # A distinct, smaller real limit (sqlite) must produce a correspondingly smaller batch size -
    # proves the function isn't just returning a fixed value regardless of what's passed in.
    assert BulkWriteBatches.get_bind_param_safe_batch_size(None, num_columns=3, max_bind_params=32766) == 10922
    assert BulkWriteBatches.get_bind_param_safe_batch_size(None, num_columns=3, max_bind_params=999) == 333
    # A caller-requested batch_size is still clamped down to the real per-backend ceiling rather
    # than trusted outright - an oversized explicit batch_size is exactly as unsafe as an
    # oversized auto-computed one.
    assert BulkWriteBatches.get_bind_param_safe_batch_size(50_000, num_columns=3, max_bind_params=32767) == 10922
    # A caller-requested batch_size already within the real limit passes through unchanged.
    assert BulkWriteBatches.get_bind_param_safe_batch_size(100, num_columns=3, max_bind_params=32767) == 100


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_batch_size_respects_real_asyncpg_bind_param_limit(db_truncate):
    """Live regression test for the wrong MAX_BIND_PARAMS=65535 constant against a real asyncpg
    connection (the bug this whole test guards against was only ever visible against the real
    driver - a 65535-derived batch size looks perfectly reasonable in isolation).

    UniqueName's INSERT carries 3 bind-parameterized columns (name/optional/other_optional - id
    is DB-generated, never in the column list). With no explicit batch_size, the old wrong
    constant computed 65535 // 3 = 21845 rows as "safe" - large enough that object_count objects
    below (11000, comfortably more than 32767 // 3 = 10922 but comfortably less than 21845) would
    have landed in a SINGLE unchunked INSERT carrying 11000 * 3 = 33000 bind parameters, which
    real asyncpg rejects outright ("the number of query arguments cannot exceed 32767"). The real
    fix (batch_size derived from db.features.max_bind_parameters = 32767) keeps every chunk at
    10922 rows, so this must complete cleanly and populate every object's real, DB-generated id.
    """
    object_count = 11_000
    objects = [UniqueName(name=f"bpl-{i}", optional="o", other_optional="oo") for i in range(object_count)]

    await UniqueName.objects.bulk_create(objects, returning=True)

    assert all(obj.id is not None for obj in objects)
    assert len({obj.id for obj in objects}) == object_count


@pytest.mark.asyncio
async def test_bulk_update_batch_size_respects_backends_real_bind_param_limit(db_truncate):
    """Live regression test for the wrong MAX_BIND_PARAMS=65535 constant, for bulk_update() this
    time - runs against whichever real backend the suite is configured for (sqlite's own real
    SQLITE_LIMIT_VARIABLE_NUMBER, or asyncpg's real wire-protocol limit), not a fixed row count
    tuned to one dialect.

    Each updated row binds 3 parameters (pk + intnum + intnum_null). With no explicit batch_size,
    the old wrong constant computed 65535 // 3 = 21845 rows as "safe" - large enough that
    object_count objects below (11000, more than either real backend's own 32767|32766 // 3 =
    10922 but less than 21845) would have landed in a SINGLE unchunked UPDATE ... FROM (VALUES
    ...) statement carrying 33000 bind parameters, more than either real driver accepts
    ("the number of query arguments cannot exceed 32767" on asyncpg, "too many SQL variables" on
    sqlite).
    """
    object_count = 11_000
    await IntFields.objects.bulk_create([IntFields(intnum=0, intnum_null=0) for _ in range(object_count)])
    objects = list(await IntFields.objects.all().order_by("id"))
    for i, obj in enumerate(objects):
        obj.intnum = i
        obj.intnum_null = i * 2

    await IntFields.objects.bulk_update(objects, fields=["intnum", "intnum_null"])

    rows = await IntFields.objects.all().order_by("id").values("intnum", "intnum_null")
    assert rows == [{"intnum": i, "intnum_null": i * 2} for i in range(object_count)]


@pytest.mark.asyncio
async def test_bulk_create_batch_size_zero_raises_clear_error(db_truncate):
    """batch_size=0 used to be silently treated as batch_size=None (no limit) by chunk()'s own
    falsy check - a caller who explicitly wrote batch_size=0 clearly wanted something enforced,
    not to have it quietly ignored. Must raise a clear hare-specific error instead."""
    with pytest.raises(QueryError, match="batch_size"):
        await UniqueName.objects.bulk_create([UniqueName(name="zero-batch")], batch_size=0)


@pytest.mark.asyncio
async def test_bulk_create_batch_size_negative_raises_clear_error(db_truncate):
    """batch_size<0 used to propagate itertools.batched's own raw "n must be at least one"
    ValueError, naming neither the batch_size parameter nor bulk_create() at all. Must raise a
    clear hare-specific error instead."""
    with pytest.raises(QueryError, match="batch_size"):
        await UniqueName.objects.bulk_create([UniqueName(name="negative-batch")], batch_size=-1)


@pytest.mark.asyncio
async def test_bulk_update_batch_size_zero_raises_clear_error(db_truncate):
    """Same fix as bulk_create()'s, for bulk_update()."""
    obj = await IntFields.objects.create(intnum=0)
    obj.intnum = 1
    with pytest.raises(QueryError, match="batch_size"):
        await IntFields.objects.bulk_update([obj], fields=["intnum"], batch_size=0)


@pytest.mark.asyncio
async def test_bulk_update_batch_size_negative_raises_clear_error(db_truncate):
    """Same fix as bulk_create()'s, for bulk_update()."""
    obj = await IntFields.objects.create(intnum=0)
    obj.intnum = 1
    with pytest.raises(QueryError, match="batch_size"):
        await IntFields.objects.bulk_update([obj], fields=["intnum"], batch_size=-5)


@pytest.mark.asyncio
async def test_bulk_update_on_a_none_queryset_writes_nothing(db_truncate):
    """.none().bulk_update() returns 0 without touching the database, like .none().update()."""
    obj = await IntFields.objects.create(intnum=1)
    obj.intnum = 2

    assert await IntFields.objects.none().bulk_update([obj], fields=["intnum"]) == 0
    assert await IntFields.objects.filter(id=obj.id).values_list("intnum", flat=True) == [1]
    assert await IntFields.objects.all().bulk_update([obj], fields=["intnum"]) == 1
    assert await IntFields.objects.filter(id=obj.id).values_list("intnum", flat=True) == [2]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_returning_gives_a_duplicate_key_to_the_first_object(db_truncate):
    """Two objects of one call sharing a conflict key: the database inserts the first and skips
    the second, so only the first may get the pk - the second one getting it made its next save()
    overwrite the first object's row."""
    first = UniqueName(name="shared", optional="first")
    second = UniqueName(name="shared", optional="second")

    await UniqueName.objects.bulk_create([first, second], ignore_conflicts=True, on_conflict=["name"], returning=True)

    row = await UniqueName.objects.get(name="shared")
    assert (first.pk, first._saved_in_db) == (row.pk, True)
    assert (second.pk, second._saved_in_db) == (None, False)
    assert row.optional == "first"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_failing_late_batch_restores_objects_of_earlier_batches(db_truncate):
    """A later batch failing rolls back the rows of earlier batches, so their objects must not
    keep the pk and saved state RETURNING gave them."""
    await UniqueName.objects.create(name="taken")
    fresh = UniqueName(name="fresh")
    colliding = UniqueName(name="taken")

    with pytest.raises(IntegrityError):
        await UniqueName.objects.bulk_create([fresh, colliding], batch_size=1, returning=True)

    assert (fresh.pk, fresh._saved_in_db) == (None, False)
    assert (colliding.pk, colliding._saved_in_db) == (None, False)
    fresh.optional = "saved later"
    await fresh.save()
    assert await UniqueName.objects.filter(name="fresh").values_list("optional", flat=True) == ["saved later"]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_failing_late_statement_restores_auto_now(db_truncate):
    """Same restore on every dialect, without RETURNING: an auto_now value set while serializing
    a rolled-back batch must go back to unset."""
    await VersionedUniqueAutoNow.objects.create(name="existing", tag="taken")
    fresh = VersionedUniqueAutoNow(name="fresh", tag="fresh")
    colliding = VersionedUniqueAutoNow(name="colliding", tag="taken")

    with pytest.raises(IntegrityError):
        await VersionedUniqueAutoNow.objects.bulk_create([fresh, colliding], batch_size=1)

    assert fresh.updated_at is None
    assert fresh._saved_in_db is False
    assert await VersionedUniqueAutoNow.objects.filter(tag="fresh").count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_maps_source_field_names_to_columns(db_truncate):
    """on_conflict/update_fields name fields - a field with source_field went into ON CONFLICT
    and the SET list under its field name, not its column."""
    await UpsertTarget.objects.create(code="a", note="old")

    await UpsertTarget.objects.bulk_create(
        [UpsertTarget(code="a", note="new")], update_fields=["note"], on_conflict=["code"]
    )

    assert await UpsertTarget.objects.all().values_list("code", "note") == [("a", "new")]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_maps_a_relation_name_to_its_key_column(db_truncate):
    """A forward FK name in on_conflict/update_fields means its key column."""
    from tests.testmodels import UniqueTogetherFieldsWithFK

    first_tournament = await Tournament.objects.create(name="first")
    second_tournament = await Tournament.objects.create(name="second")
    await UniqueTogetherFieldsWithFK.objects.create(text="a", tournament=first_tournament)

    await UniqueTogetherFieldsWithFK.objects.bulk_create(
        [UniqueTogetherFieldsWithFK(text="a", tournament=first_tournament)],
        update_fields=["text"],
        on_conflict=["text", "tournament"],
    )
    await UniqueTogetherFieldsWithFK.objects.bulk_create(
        [UniqueTogetherFieldsWithFK(text="b", tournament=second_tournament)],
        update_fields=["tournament"],
        on_conflict=["text", "tournament_id"],
    )

    assert sorted(await UniqueTogetherFieldsWithFK.objects.all().values_list("text", "tournament_id")) == [
        ("a", first_tournament.pk),
        ("b", second_tournament.pk),
    ]


@pytest.mark.asyncio
async def test_bulk_create_upsert_rejects_an_unknown_field_name(db_truncate):
    with pytest.raises(FieldError, match="missing"):
        UpsertTarget.objects.bulk_create([UpsertTarget(code="a")], update_fields=["missing"], on_conflict=["code"])
    with pytest.raises(FieldError, match="missing"):
        UpsertTarget.objects.bulk_create([UpsertTarget(code="a")], update_fields=["note"], on_conflict=["missing"])


@pytest.mark.asyncio
async def test_bulk_create_upsert_rejects_the_tenant_relation_by_its_relation_name(db_truncate):
    """Meta.tenant_field is the FK's shadow column - naming the relation itself in update_fields
    must hit the same guard."""
    from tests.testmodels import TenantFkOrder

    with pytest.raises(QueryError, match="company_id"):
        TenantFkOrder.objects.all_tenants().bulk_create(
            [TenantFkOrder(title="x", company_id=1)], update_fields=["company"], on_conflict=["id"]
        )


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_updates_a_db_default_field_to_its_default(db_truncate):
    """A field in update_fields every object leaves to its db_default used to be dropped from the
    SET list - silently not updated, or a raw builder error when it was the only one."""
    await UpsertTarget.objects.create(code="a", note="old", counter=100)

    await UpsertTarget.objects.bulk_create(
        [UpsertTarget(code="a", note="new")], update_fields=["note", "counter"], on_conflict=["code"]
    )
    assert await UpsertTarget.objects.filter(code="a").values_list("note", "counter") == [("new", 7)]

    await UpsertTarget.objects.filter(code="a").update(counter=100)
    await UpsertTarget.objects.bulk_create([UpsertTarget(code="a")], update_fields=["counter"], on_conflict=["code"])
    assert await UpsertTarget.objects.filter(code="a").values_list("counter", flat=True) == [7]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_upsert_returning_reads_back_the_bumped_version(db_truncate):
    """The upsert bumps Meta.optimistic_lock_field on the conflicting row - without reading it back, the
    object's next save() raised a false StaleObjectError."""
    await UpsertTarget.objects.create(code="a", note="old")
    upserted = UpsertTarget(code="a", note="upsert")

    await UpsertTarget.objects.bulk_create([upserted], update_fields=["note"], on_conflict=["code"], returning=True)

    row = await UpsertTarget.objects.get(code="a")
    assert (upserted.pk, upserted.version) == (row.pk, row.version) == (row.pk, 1)
    upserted.note = "edited"
    await upserted.save()
    assert await UpsertTarget.objects.filter(code="a").values_list("note", "version") == [("edited", 2)]


@pytest.mark.asyncio
async def test_bulk_update_on_a_table_named_like_its_values_alias(db_truncate):
    """The VALUES table was always aliased "v" - a table of that name failed on Postgres, and a
    column named like one of the VALUES table's c<N> columns was ambiguous in WHERE."""
    from tests.testmodels import BulkUpdateValuesAliasClash

    objects = [await BulkUpdateValuesAliasClash.objects.create(c0=1, c1=1) for _ in range(2)]
    for obj in objects:
        obj.c1 = 2

    assert await BulkUpdateValuesAliasClash.objects.filter(c0=1).bulk_update(objects, fields=["c1"]) == 2

    assert await BulkUpdateValuesAliasClash.objects.all().order_by("id").values_list("c1", "version") == [
        (2, 1),
        (2, 1),
    ]
    assert [obj.version for obj in objects] == [1, 1]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_bumps_auto_now_of_a_conflicting_row(db_truncate):
    """ON CONFLICT DO UPDATE writes a conflicting row's auto_now fields like save()/bulk_update()/
    update() do - it used to bump only Meta.optimistic_lock_field."""
    existing = await VersionedUniqueAutoNow.objects.create(name="a", tag="t-upsert-auto-now")
    original_updated_at = existing.updated_at

    await VersionedUniqueAutoNow.objects.bulk_create(
        [VersionedUniqueAutoNow(name="b", tag="t-upsert-auto-now")], update_fields=["name"], on_conflict=["tag"]
    )

    upserted = await VersionedUniqueAutoNow.objects.get(tag="t-upsert-auto-now")
    assert upserted.name == "b"
    assert upserted.version == existing.version + 1
    assert upserted.updated_at > original_updated_at
