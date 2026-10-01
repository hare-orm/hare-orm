"""HydrateAccelerator.disable() - the optional rust.hydrate
accelerator's compiled extension can go stale relative to the Python call sites that invoke it
(a signature change after `maturin develop --release` was run against an older commit). Before
this fix, a TypeError/AttributeError from an actual hydrate_rows()/serialize_rows() call
propagated straight up and crashed the query instead of falling back to the pure-Python path
every caller already has. These tests substitute a deliberately-broken fake accelerator - they
don't need rust.hydrate to actually be built, unlike tests/test_rust_hydrate_parity.py.
"""

import pytest

from hare.fields.data.json import JsonCodec
from hare.query.plans.statement_plans import StatementPlans
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.model_columns import ModelColumns
from hare.query.statements.write.bulk_write_batches import BulkWriteBatches
from tests.testmodels import IntFields, JSONFields


class BrokenHydrate:
    def FieldCodec(self, *args, **kwargs):
        raise TypeError("FieldCodec() takes 5 positional arguments but 6 were given")


@pytest.fixture
def broken_hydrate():
    original = HydrateAccelerator.module
    original_warned = HydrateAccelerator.incompatibility_logged
    HydrateAccelerator.module = BrokenHydrate()
    HydrateAccelerator.incompatibility_logged = False
    StatementPlans.forget_all()
    yield
    StatementPlans.forget_all()
    HydrateAccelerator.module = original
    HydrateAccelerator.incompatibility_logged = original_warned


@pytest.mark.asyncio
async def test_select_falls_back_to_python_path_on_hydrate_rows_signature_mismatch(db, broken_hydrate):
    await IntFields.objects.create(intnum=1)

    obj = await IntFields.objects.get(intnum=1)

    assert obj.intnum == 1
    assert HydrateAccelerator.module is None
    assert HydrateAccelerator.incompatibility_logged is True


@pytest.mark.asyncio
async def test_bulk_create_falls_back_to_python_path_on_serialize_rows_signature_mismatch(db, broken_hydrate):
    await IntFields.objects.bulk_create([IntFields(intnum=1), IntFields(intnum=2)])

    rows = await IntFields.objects.all().order_by("intnum").values_list("intnum", flat=True)

    assert rows == [1, 2]
    assert HydrateAccelerator.module is None
    assert HydrateAccelerator.incompatibility_logged is True


@pytest.mark.asyncio
async def test_disabling_the_accelerator_is_only_logged_once(db, broken_hydrate, caplog):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    await IntFields.objects.get(intnum=1)
    await IntFields.objects.get(intnum=2)

    warnings = [r for r in caplog.records if "rust.native.rows accelerator" in r.message]
    assert len(warnings) == 1


class ObjectFaultHydrate:
    def FieldCodec(self, *args, **kwargs):
        raise AttributeError("'IntFields' object has no attribute 'intnum'")


@pytest.fixture
def object_fault_hydrate():
    original = HydrateAccelerator.module
    original_warned = HydrateAccelerator.incompatibility_logged
    fake_hydrate = ObjectFaultHydrate()
    HydrateAccelerator.module = fake_hydrate
    HydrateAccelerator.incompatibility_logged = False
    StatementPlans.forget_all()
    yield fake_hydrate
    StatementPlans.forget_all()
    HydrateAccelerator.module = original
    HydrateAccelerator.incompatibility_logged = original_warned


@pytest.mark.asyncio
async def test_serialize_error_the_objects_cause_keeps_the_accelerator(db, object_fault_hydrate):
    """The pure-Python path fails on the same object, so the error was the caller's, not a stale
    rust.hydrate build's - it propagates and the accelerator stays on."""
    obj = IntFields(intnum=1)
    object.__delattr__(obj, "intnum")
    types = IntFields.get_connection().dialect.types

    with pytest.raises(AttributeError):
        BulkWriteBatches.serialize_instances(IntFields, types, [obj], ["intnum"])

    assert HydrateAccelerator.module is object_fault_hydrate
    assert HydrateAccelerator.incompatibility_logged is False


@pytest.mark.asyncio
async def test_serialize_error_the_pure_python_path_handles_disables_the_accelerator(db, object_fault_hydrate):
    types = IntFields.get_connection().dialect.types

    rows = BulkWriteBatches.serialize_instances(IntFields, types, [IntFields(intnum=1)], ["intnum"])

    assert rows == [[1]]
    assert HydrateAccelerator.module is None


@pytest.mark.asyncio
async def test_select_error_the_rows_cause_keeps_the_accelerator(db, broken_hydrate, monkeypatch):
    """The pure-Python path fails on the same rows too, so rust.hydrate isn't switched off."""
    await IntFields.objects.create(intnum=1)
    fake_hydrate = HydrateAccelerator.module

    def raise_type_error(*args, **kwargs):
        raise TypeError("row can't be hydrated")

    monkeypatch.setattr(ModelColumns, "get_hydrate_function", lambda self: raise_type_error)

    with pytest.raises(TypeError, match="row can't be hydrated"):
        await IntFields.objects.all()

    assert HydrateAccelerator.module is fake_hydrate


@pytest.mark.asyncio
async def test_accelerated_select_sets_the_connection_without_a_second_pass(db, monkeypatch):
    """Bug: after rust.hydrate built the objects, a second Python pass walked all of them again
    just to set each one's connection. A build taking connection_name sets it while building
    them - the compiled Python reader is never asked for."""
    hydrate = pytest.importorskip("rust.native.rows")
    if "connection_name" not in (getattr(hydrate.ModelReader.read, "__text_signature__", None) or ""):
        pytest.skip("this rust.hydrate build predates connection_name")
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    def fail_second_pass(self):
        raise AssertionError("the Python row reader ran for an accelerated select")

    monkeypatch.setattr(ModelColumns, "get_hydrate_function", fail_second_pass)
    rows = await IntFields.objects.all()
    assert len(rows) == 2
    connection_name = IntFields.get_connection().connection_name
    assert {row._db_connection_name for row in rows} == {connection_name}


def _accelerator_reads_json_itself() -> bool:
    hydrate = pytest.importorskip("rust.native.rows")
    return JsonCodec.orjson is not None and "connection_name" in (
        getattr(hydrate.ModelReader.read, "__text_signature__", None) or ""
    )


@pytest.mark.asyncio
async def test_accelerated_select_decodes_plain_json_without_the_python_wrapper(db, monkeypatch):
    """Bug: every JSON value read through rust.hydrate went through JsonCodec.loads - a Python
    call running has_long_integer() before orjson - about a microsecond per row. The accelerator
    runs that scan itself and calls orjson directly."""
    if not _accelerator_reads_json_itself():
        pytest.skip("this rust.hydrate build predates its own JSON scan, or orjson is missing")
    await JSONFields.objects.create(data={"tags": ["a", "b"], "count": 3})

    def fail_scan(text):
        raise AssertionError("JsonCodec.has_long_integer() ran for an accelerated select")

    monkeypatch.setattr(JsonCodec, "has_long_integer", fail_scan)
    # Only the plain JSONField - the model's pydantic-typed ones decode through from_db_value.
    (row,) = await JSONFields.objects.all().only("id", "data")
    assert row.data == {"tags": ["a", "b"], "count": 3}


@pytest.mark.asyncio
async def test_accelerated_select_reads_a_long_json_integer_exactly(db):
    """A value holding an integer too long for orjson still goes through JsonCodec.loads, which
    reads it exactly instead of as a float."""
    long_integer = 123456789012345678901234567890
    await JSONFields.objects.create(data={"big": long_integer, "items": [long_integer]})
    (row,) = await JSONFields.objects.all()
    assert row.data == {"big": long_integer, "items": [long_integer]}
    assert type(row.data["big"]) is int


@pytest.mark.asyncio
async def test_select_related_rows_hydrate_through_the_accelerator(db, monkeypatch):
    """select_related() rows went through the pure-Python loop, one related instance per row
    and relation; rust.hydrate reads each model's slice of every row now - a LEFT JOIN that
    matched nothing still gives None, and a nested relation hangs off its parent."""
    from tests.testmodels import DoubleFK

    hydrate = pytest.importorskip("rust.native.rows")
    if "column_offset" not in (getattr(hydrate.ModelReader.read, "__text_signature__", None) or ""):
        pytest.skip("this rust.hydrate build predates column_offset")
    if HydrateAccelerator.module is None or not DoubleFK.get_connection().features.supports_positional_rows:
        pytest.skip("rows of this connection aren't read by the accelerator")
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    def fail_python_hydration(self):
        raise AssertionError("a select_related() row was hydrated in Python")

    monkeypatch.setattr(ModelColumns, "get_hydrate_function", fail_python_hydration)
    rows = {row.name: row for row in await DoubleFK.objects.all().select_related("left", "left__left")}
    assert rows["root"].left.name == "middle"
    assert rows["root"].left.left.name == "leaf"
    assert rows["root"].left.left.pk == leaf.pk
    assert rows["middle"].left.name == "leaf"
    assert rows["middle"].left.left is None
    assert rows["leaf"].left is None
    connection_name = DoubleFK.get_connection().connection_name
    assert rows["root"].left._db_connection_name == connection_name
    assert rows["root"].pk == root.pk and rows["middle"].pk == middle.pk


@pytest.mark.asyncio
async def test_nested_select_related_selects_each_relation_once(db):
    """Bug: select_related("left", "left__left") walked "left" twice and selected its columns
    twice - the row held more columns than its buckets described."""
    from hare.contrib.test import capture_queries
    from tests.testmodels import DoubleFK

    await DoubleFK.objects.create(name="leaf", left=None)
    async with capture_queries() as counter:
        await DoubleFK.objects.all().select_related("left", "left__left")
    (select_sql,) = [query for query in counter.queries if query.lstrip().upper().startswith("SELECT")]
    unquoted_sql = select_sql.replace('"', "").replace("`", "")
    # Once as the column, once as its label.
    assert unquoted_sql.count("doublefk__left.name") == 2
