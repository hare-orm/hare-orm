import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.lookups.encoders import PostgresqlValueEncoders
from hare.dialects.registry import DialectRegistry
from hare.exceptions import IntegrityError, UnSupportedError, ValidationError
from tests import testmodels_postgres as testmodels
from tests.utils.database_under_test import DatabaseUnderTest


def test_sql_type_raises_unsupported_error_for_non_postgres_dialect():
    """A Postgres array type (e.g. TEXT[]) can't be resolved for another dialect's DDL - a model
    with an ArrayField that ends up on a SQLite connection must raise a clear ConfigurationError
    at schema-generation time instead of silently generating garbage DDL."""
    field = ArrayField(base_field=fields.TextField())
    field.model_field_name = "tags"
    with pytest.raises(UnSupportedError, match="ArrayField.*tags.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


def test_sql_type_still_resolves_for_postgres_dialect():
    field = ArrayField(base_field=fields.TextField())
    field.model_field_name = "tags"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "TEXT[]"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_empty(db_array_fields):
    """Test that creating without required array field raises IntegrityError."""
    with pytest.raises(IntegrityError):
        await testmodels.ArrayFields.objects.create()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_create(db_array_fields):
    """Test array field creation and retrieval."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0])
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array == [0]
    assert obj.array_null is None
    await obj.save()
    obj2 = await testmodels.ArrayFields.objects.get(id=obj.id)
    assert obj == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_of_enum_round_trips_as_enum_members(db_array_fields):
    """ArrayField must apply base_field's own to_db_value/from_db_value to each element, not
    just coerce the outer list - elements of a CharEnumField array must come back as enum
    members, matching what a plain (non-array) CharEnumField column already does."""
    obj0 = await testmodels.ArrayFields.objects.create(
        array=[0], array_enum=[testmodels.Color.RED, testmodels.Color.BLUE]
    )
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_enum == [testmodels.Color.RED, testmodels.Color.BLUE]
    assert all(isinstance(item, testmodels.Color) for item in obj.array_enum)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_of_json_round_trips(db_array_fields):
    """rust_pg had no decode arm for the _json/_jsonb array OIDs, so a jsonb[] column fell
    through to the generic unknown-type fallback and came back as one hex-encoded blob of the
    array's raw wire bytes - ArrayField.from_db_value then iterated that string character by
    character instead of element by element."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0], array_json=[{"a": 1, "b": "x"}, {"c": [1, 2, 3]}])
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_json == [{"a": 1, "b": "x"}, {"c": [1, 2, 3]}]


def test_array_to_python_value_rejects_a_bare_string():
    """A bare str is itself iterable character-by-character - from_db_value used to accept
    one with no shape check, silently producing a plausible-looking but wrong list instead of
    surfacing the real decoding bug (or caller mistake) that produced a scalar in the first
    place."""
    field = testmodels.ArrayFields._meta.fields_map["array_str"]
    with pytest.raises(ValidationError):
        field.from_db_value("hello")


def test_array_to_db_value_sorts_a_set_for_deterministic_order():
    """A plain set has no defined iteration order (PYTHONHASHSEED-dependent for str elements) -
    to_db_value used to store it in whatever order the set happened to iterate in, so the same
    logical set of values came back in a different column order across process runs."""
    field = testmodels.ArrayFields._meta.fields_map["array_str"]
    assert field.to_db_value({"c", "a", "b"}, testmodels.ArrayFields) == ["a", "b", "c"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_field_validators_run_on_save(db_array_fields):
    """ArrayField.to_db_value never called self.validate(value) (unlike PostGISField/RangeField
    in the same file) - array-level validators (e.g. a max-element-count check) were declared but
    silently never enforced on save."""
    with pytest.raises(ValidationError):
        await testmodels.ArrayFields.objects.create(array=[0], array_max_length_2=[1, 2, 3])

    obj = await testmodels.ArrayFields.objects.create(array=[0], array_max_length_2=[1, 2])
    assert obj.array_max_length_2 == [1, 2]


def test_to_db_value_wraps_non_iterable():
    """ArrayField.to_db_value does `[self.base_field.to_db_value(element, instance) for element
    in value]` with no check that `value` is actually iterable first - a non-iterable value
    (e.g. a plain int from a raw attribute assignment) raised a bare TypeError instead of the
    framework's own catchable ValidationError, same unwrapped-exception bug class as every other
    field's own conversion step fixed this session."""
    field = testmodels.ArrayFields._meta.fields_map["array"]
    with pytest.raises(ValidationError):
        field.to_db_value(123, testmodels.ArrayFields)


def test_range_field_to_db_value_wraps_wrong_shape():
    """RangeField.to_db_value's non-Range branch does `lower, upper = value` with no check that
    `value` actually unpacks to exactly 2 elements first - same unwrapped-exception bug class as
    PostGISField's own identical unpacking pattern (tests/dialects/postgresql/test_postgis.py) and
    every other field's own conversion step fixed this session."""
    from hare.dialects.postgresql.fields.ranges import IntRangeField

    field = IntRangeField()
    with pytest.raises(ValidationError):
        field.to_db_value((1,), testmodels.ArrayFields)
    with pytest.raises(ValidationError):
        field.to_db_value((1, 2, 3), testmodels.ArrayFields)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_update(db_array_fields):
    """Test array field update."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0])
    await testmodels.ArrayFields.objects.filter(id=obj0.id).update(array=[1])
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array == [1]
    assert obj.array_null is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values(db_array_fields):
    """Test array field in values()."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0])
    values = await testmodels.ArrayFields.objects.get(id=obj0.id).values("array")
    assert values["array"] == [0]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_list(db_array_fields):
    """Test array field in values_list()."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0])
    values = await testmodels.ArrayFields.objects.get(id=obj0.id).values_list("array", flat=True)
    assert values == [0]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_eq_filter(db_array_fields):
    """Test equality filter on array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3])
    obj2 = await testmodels.ArrayFields.objects.create(array=[1, 2])

    found = await testmodels.ArrayFields.objects.filter(array=[1, 2, 3]).first()
    assert found == obj1

    found = await testmodels.ArrayFields.objects.filter(array=[1, 2]).first()
    assert found == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_not_filter(db_array_fields):
    """Test not filter on array field."""
    await testmodels.ArrayFields.objects.create(array=[1, 2, 3])
    obj2 = await testmodels.ArrayFields.objects.create(array=[1, 2])

    found = await testmodels.ArrayFields.objects.filter(array__not=[1, 2, 3]).first()
    assert found == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_isnull_filter(db_array_fields):
    """Test isnull/not_isnull filters on a nullable array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3], array_null=[4, 5])
    obj2 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3])

    assert await testmodels.ArrayFields.objects.filter(array_null__isnull=True).first() == obj2
    assert await testmodels.ArrayFields.objects.filter(array_null__isnull=False).first() == obj1
    assert await testmodels.ArrayFields.objects.filter(array_null__not_isnull=True).first() == obj1
    assert await testmodels.ArrayFields.objects.filter(array_null__not_isnull=False).first() == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_isnull_filter_rejects_non_bool_value(db_array_fields):
    """bool_encoder used to coerce via bare bool(value) - a truthy non-bool like "false" used to
    silently invert the filter instead of raising."""
    from hare.exceptions import UnSupportedError

    with pytest.raises(UnSupportedError):
        await testmodels.ArrayFields.objects.filter(array_null__isnull="false").first()
    with pytest.raises(UnSupportedError):
        await testmodels.ArrayFields.objects.filter(array_null__not_isnull="false").first()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_ints(db_array_fields):
    """Test contains filter on integer array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3])
    obj2 = await testmodels.ArrayFields.objects.create(array=[2, 3])
    await testmodels.ArrayFields.objects.create(array=[4, 5, 6])

    found = await testmodels.ArrayFields.objects.filter(array__contains=[2])
    assert found == [obj1, obj2]

    found = await testmodels.ArrayFields.objects.filter(array__contains=[10])
    assert found == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_smallints(db_array_fields):
    """Test contains filter on smallint array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[], array_smallint=[1, 2, 3])

    found = await testmodels.ArrayFields.objects.filter(array_smallint__contains=[2]).first()
    assert found == obj1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_strs(db_array_fields):
    """Test contains filter on string array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array_str=["a", "b", "c"], array=[])

    found = await testmodels.ArrayFields.objects.filter(array_str__contains=["a", "b", "c"])
    assert found == [obj1]

    found = await testmodels.ArrayFields.objects.filter(array_str__contains=["a", "b"])
    assert found == [obj1]

    found = await testmodels.ArrayFields.objects.filter(array_str__contains=["a", "b", "c", "d"])
    assert found == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_encoder_applies_base_field_to_db_value_per_element(db_array_fields):
    """array_encoder() backs the __contains/__contained_by/__overlap/equality filters and used
    to build its Array(...) literal straight from the raw filter value - it never ran
    base_field's own to_db_value() on each element, so e.g. an array of CharEnumField values
    reached the driver as raw enum members instead of their already-coerced .value strings, even
    though storing that same array via create()/save() was already correctly coerced through
    ArrayField.to_db_value()."""
    field = testmodels.ArrayFields._meta.fields_map["array_enum"]

    encoded = PostgresqlValueEncoders.encode_array(
        [testmodels.Color.RED, testmodels.Color.BLUE], None, field, POSTGRESQL_DIALECT
    )

    array_term = encoded.args[0]
    assert array_term.original_value == ["red", "blue"]
    assert all(type(v) is str for v in array_term.original_value)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contained_by_ints(db_array_fields):
    """Test contained_by filter on integer array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[1])
    obj2 = await testmodels.ArrayFields.objects.create(array=[1, 2])
    obj3 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3])

    found = await testmodels.ArrayFields.objects.filter(array__contained_by=[1, 2, 3])
    assert found == [obj1, obj2, obj3]

    found = await testmodels.ArrayFields.objects.filter(array__contained_by=[1, 2])
    assert found == [obj1, obj2]

    found = await testmodels.ArrayFields.objects.filter(array__contained_by=[1])
    assert found == [obj1]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contained_by_strs(db_array_fields):
    """Test contained_by filter on string array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array_str=["a"], array=[])
    obj2 = await testmodels.ArrayFields.objects.create(array_str=["a", "b"], array=[])
    obj3 = await testmodels.ArrayFields.objects.create(array_str=["a", "b", "c"], array=[])

    found = await testmodels.ArrayFields.objects.filter(array_str__contained_by=["a", "b", "c", "d"])
    assert found == [obj1, obj2, obj3]

    found = await testmodels.ArrayFields.objects.filter(array_str__contained_by=["a", "b"])
    assert found == [obj1, obj2]

    found = await testmodels.ArrayFields.objects.filter(array_str__contained_by=["x", "y", "z"])
    assert found == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_overlap_ints(db_array_fields):
    """Test overlap filter on integer array field."""
    obj1 = await testmodels.ArrayFields.objects.create(array=[1, 2, 3])
    obj2 = await testmodels.ArrayFields.objects.create(array=[2, 3, 4])
    obj3 = await testmodels.ArrayFields.objects.create(array=[3, 4, 5])

    found = await testmodels.ArrayFields.objects.filter(array__overlap=[1, 2])
    assert found == [obj1, obj2]

    found = await testmodels.ArrayFields.objects.filter(array__overlap=[4])
    assert found == [obj2, obj3]

    found = await testmodels.ArrayFields.objects.filter(array__overlap=[1, 2, 3, 4, 5])
    assert found == [obj1, obj2, obj3]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_length(db_array_fields):
    """Test array length filter."""
    await testmodels.ArrayFields.objects.create(array=[1, 2, 3])
    await testmodels.ArrayFields.objects.create(array=[1])
    await testmodels.ArrayFields.objects.create(array=[1, 2])
    await testmodels.ArrayFields.objects.create(array=[])

    found = await testmodels.ArrayFields.objects.filter(array__len=3).values_list("array", flat=True)
    assert list(found) == [[1, 2, 3]]

    found = await testmodels.ArrayFields.objects.filter(array__len=1).values_list("array", flat=True)
    assert list(found) == [[1]]

    # Postgres's array_length() returns NULL (not 0) for an empty array, so a bare
    # `array_length(field, 1) = 0` never matched a genuinely empty array - regression test for
    # that fix (postgres_array_length() now coalesces to 0 first).
    found = await testmodels.ArrayFields.objects.filter(array__len=0).values_list("array", flat=True)
    assert list(found) == [[]]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_length_zero_does_not_match_null_column(db_array_fields):
    """array_length() returns NULL both for a genuinely empty array and for a NULL column, so
    coalescing it to 0 for __len=0 must not also make a NULL column match - a NULL column has no
    length at all and shouldn't match any __len value, including 0."""
    empty = await testmodels.ArrayFields.objects.create(array=[1], array_null=[])
    await testmodels.ArrayFields.objects.create(array=[1], array_null=None)

    found = await testmodels.ArrayFields.objects.filter(array_null__len=0).values_list("id", flat=True)
    assert list(found) == [empty.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_filter_matches_by_zero_indexed_position(db_array_fields):
    """__item=(index, value) is 0-indexed (Python convention) - translated internally to
    Postgres's own 1-indexed array subscript."""
    obj = await testmodels.ArrayFields.objects.create(array=[10, 20, 30])
    await testmodels.ArrayFields.objects.create(array=[1, 2, 3])

    found = await testmodels.ArrayFields.objects.filter(array__item=(0, 10)).values_list("id", flat=True)
    assert list(found) == [obj.id]

    found = await testmodels.ArrayFields.objects.filter(array__item=(2, 30)).values_list("id", flat=True)
    assert list(found) == [obj.id]

    found = await testmodels.ArrayFields.objects.filter(array__item=(0, 999)).values_list("id", flat=True)
    assert list(found) == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_filter_matches_by_negative_index(db_array_fields):
    """A negative index used to render as a bare `field[index + 1]` subscript, e.g. -1 ->
    `field[0]` - always out of Postgres's 1-indexed range and so always NULL, silently matching
    no row regardless of the array's actual contents. Now counts from the end of the array
    (Python convention: -1 is the last element), same as `some_list[-1]`."""
    obj = await testmodels.ArrayFields.objects.create(array=[10, 20, 30])
    empty = await testmodels.ArrayFields.objects.create(array=[])

    found = await testmodels.ArrayFields.objects.filter(array__item=(-1, 30)).values_list("id", flat=True)
    assert list(found) == [obj.id]

    found = await testmodels.ArrayFields.objects.filter(array__item=(-3, 10)).values_list("id", flat=True)
    assert list(found) == [obj.id]

    found = await testmodels.ArrayFields.objects.filter(array__item=(-4, 10)).values_list("id", flat=True)
    assert list(found) == []

    found = await testmodels.ArrayFields.objects.filter(id=empty.id, array__item=(-1, 1)).values_list("id", flat=True)
    assert list(found) == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_annotate_returns_zero_indexed_element(db_array_fields):
    """ArrayItem(field, index) is the annotate()-side counterpart to __item=(index, value) -
    same 0-indexed convention."""
    from hare.dialects.postgresql.functions.array import ArrayItem
    from hare.query.expressions import F

    obj = await testmodels.ArrayFields.objects.create(array=[10, 20, 30])

    row = await testmodels.ArrayFields.objects.filter(id=obj.id).annotate(first=ArrayItem("array", 0)).values("first")
    assert row == [{"first": 10}]

    row = (
        await testmodels.ArrayFields.objects.filter(id=obj.id)
        .annotate(second=ArrayItem(F("array"), 1))
        .values("second")
    )
    assert row == [{"second": 20}]

    row = await testmodels.ArrayFields.objects.filter(id=obj.id).annotate(last=ArrayItem("array", -1)).values("last")
    assert row == [{"last": 30}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_equality_and_contains_lookups_work_on_a_generated_array_column(db_array_fields):
    """Two distinct bugs, both around GeneratedField.output_field never being treated as fully
    equivalent to a real ArrayField:

    1. GeneratedField.output_field is its own separate Field instance, never bound into
       fields_map - ModelMeta.__new__'s field.model = new_class binding loop only ever walked
       fields_map, so output_field.model stayed None forever. array_encoder() (the __contains/
       __overlap/equality encoder for ArrayField) calls field.get_db_field_type(), which reads
       self.model._meta.db - a raw AttributeError on None._meta for every filter against a
       GeneratedField(output_field=ArrayField(...)) column, confirmed live before this fix.
    2. Even after fixing (1), calling the SAME filter key twice with two DIFFERENT values (e.g.
       tags__contains=[1] then tags__contains=[9] below) hit QUERY_SHAPE_CACHE's cache-hit
       replay path on the second call - which stored the WRAPPED GeneratedField object itself
       (not output_field) in its ArrayValueRef, so the replay's own array_encoder() call crashed
       on `field.base_field` (a GeneratedField genuinely has no such attribute) even though the
       FIRST call for the same key succeeded. __len/__isnull don't need either fix (they never
       reach get_db_field_type()/base_field), so this test specifically exercises the ones that
       needed both."""
    obj = await testmodels.GeneratedArrayThing.objects.create(a=1, b=2)
    await obj.refresh_from_db()
    assert obj.tags == [1, 2]

    assert await testmodels.GeneratedArrayThing.objects.filter(tags=[1, 2]) == [obj]
    assert await testmodels.GeneratedArrayThing.objects.filter(tags__contains=[1]) == [obj]
    # Same filter key as above, different value - exercises the QUERY_SHAPE_CACHE cache-hit
    # replay path specifically (see bug 2 above), not just the first-build path bug 1 alone
    # would already cover.
    assert await testmodels.GeneratedArrayThing.objects.filter(tags__contains=[9]) == []
    assert await testmodels.GeneratedArrayThing.objects.filter(tags__contained_by=[1, 2, 3]) == [obj]
    assert await testmodels.GeneratedArrayThing.objects.filter(tags__overlap=[2, 5]) == [obj]
    assert await testmodels.GeneratedArrayThing.objects.filter(tags__overlap=[5, 6]) == []


# ---------------------------------------------------------------------------
# Nested (2D+) ArrayField - regression coverage for a rust_pg-specific panic (see rust/pg/src/
# value.rs's own encode_nd_array/array_shape_and_leaves doc comments) that used to kill the whole
# connection, not just the one query, whenever a nested ArrayField value was bound - reproduced on
# both INSERT (encode_nd_array's own path) and __contains/__overlap (array_encoder uses the same
# ToSql). asyncpg already handled every one of these correctly before this fix; only rust_pg's own
# encoding was broken.
# ---------------------------------------------------------------------------


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_2d_round_trips(db_array_fields):
    obj0 = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4]])
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_nested == [[1, 2], [3, 4]]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_3d_round_trips(db_array_fields):
    value = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
    obj0 = await testmodels.ArrayFields.objects.create(array=[0], array_nested_3d=value)
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_nested_3d == value


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_2d_with_empty_rows_round_trips(db_array_fields):
    """Confirmed against asyncpg (the reference backend): Postgres itself collapses ANY
    zero-element array down to a plain empty array on storage, discarding the `ndim`/per-dimension
    sizes the client sent - `[[], []]` and `[]` are indistinguishable once stored, so `[]` is the
    correct round-trip value on both backends, not `[[], []]`."""
    obj0 = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[], []])
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_nested == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_2d_null_field_round_trips(db_array_fields):
    obj0 = await testmodels.ArrayFields.objects.create(array=[0], array_nested=None)
    obj = await testmodels.ArrayFields.objects.get(id=obj0.id)
    assert obj.array_nested is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_2d_contains_filter(db_array_fields):
    obj1 = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4]])
    await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[9, 9], [8, 8]])

    found = await testmodels.ArrayFields.objects.filter(array_nested__contains=[[1, 2]])
    assert found == [obj1]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_2d_overlap_filter(db_array_fields):
    obj1 = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4]])
    await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[9, 9], [8, 8]])

    found = await testmodels.ArrayFields.objects.filter(array_nested__overlap=[[1, 2]])
    assert found == [obj1]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_ragged_nested_array_raises_operational_error_not_a_panic(db_array_fields):
    """A ragged sub-array (differing sub-array lengths) is rejected by Postgres itself for an
    ARRAY[...] literal - both backends must surface it as the same clean OperationalError, never
    a raw panic/crash."""
    from hare.exceptions import OperationalError

    with pytest.raises(OperationalError):
        await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3]])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_connection_survives_a_ragged_nested_array_insert_attempt(db_array_fields):
    """The regression this whole suite exists for: rust_pg used to panic while encoding a ragged
    (or any genuinely nested) array bind parameter, and that panic left tokio_postgres::Client's
    shared per-connection encode buffer uncleared - the NEXT query on the exact same physical
    connection then sent its message appended after the previous attempt's leftover bytes and the
    server closed the connection as malformed input (DBConnectionError on a query that had nothing
    wrong with it). The connection pool is pinned to a single connection here so every query in
    this test is provably the same physical connection (confirmed via pg_backend_pid()), not one
    the pool silently swapped out for a fresh one."""
    import os

    from hare import Connections
    from hare.contrib.test.helpers import hare_test_context
    from hare.exceptions import OperationalError

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("ArrayFields require PostgreSQL")
    pinned_pool_db_url = db_url + ("&" if "?" in db_url else "?") + "min_size=1&max_size=1"

    async with hare_test_context(
        modules=["tests.testmodels_postgres"],
        db_url=pinned_pool_db_url,
        app_label="models",
        connection_label="models",
    ):
        conn = Connections.get("models")
        pid_before = (await conn.execute_dicts("SELECT pg_backend_pid() AS pid"))[0]["pid"]

        with pytest.raises(OperationalError):
            await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3]])

        pid_after = (await conn.execute_dicts("SELECT pg_backend_pid() AS pid"))[0]["pid"]
        assert pid_after == pid_before, "the pool handed out a different connection - not a same-connection check"

        # The real assertion: a normal query on this exact connection still works.
        obj = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4]])
        assert obj.array_nested == [[1, 2], [3, 4]]


def test_element_validation_error_names_the_array_field_and_position():
    field = ArrayField(base_field=fields.SmallIntField())
    field.model_field_name = "small"
    with pytest.raises(ValidationError, match=r"^small\[1\]: Value should be less or equal to 32767"):
        field.to_db_value([1, 70000], testmodels.ArrayFields)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_len_counts_rows_not_every_element(db_array_fields):
    nested = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4], [5, 6]])
    empty = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[])
    await testmodels.ArrayFields.objects.create(array=[0], array_nested=None)

    assert await testmodels.ArrayFields.objects.filter(array_nested__len=3).values_list("id", flat=True) == [nested.id]
    assert await testmodels.ArrayFields.objects.filter(array_nested__len=6).values_list("id", flat=True) == []
    assert await testmodels.ArrayFields.objects.filter(array_nested__len=0).values_list("id", flat=True) == [empty.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_on_a_nested_array_is_a_whole_row(db_array_fields):
    from hare.dialects.postgresql.functions.array import ArrayItem

    obj = await testmodels.ArrayFields.objects.create(
        array=[0], array_nested=[[1, 2], [3, 4]], array_nested_text=[['a "q"', "b\\c"], ["{x}", "y,z"]]
    )
    await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[9, 9], [8, 8]])
    queryset = testmodels.ArrayFields.objects.filter(id=obj.id)

    assert await queryset.annotate(row=ArrayItem("array_nested", 0)).values_list("row", flat=True) == [[1, 2]]
    assert await queryset.annotate(row=ArrayItem("array_nested", -1)).values_list("row", flat=True) == [[3, 4]]
    assert await queryset.annotate(row=ArrayItem("array_nested", 5)).values_list("row", flat=True) == [None]
    assert await queryset.annotate(row=ArrayItem("array_nested_text", 0)).values_list("row", flat=True) == [
        ['a "q"', "b\\c"]
    ]
    assert await queryset.annotate(row=ArrayItem("array_nested_text", 1)).values_list("row", flat=True) == [
        ["{x}", "y,z"]
    ]

    assert await testmodels.ArrayFields.objects.filter(array_nested__item=(0, [1, 2])).values_list(
        "id", flat=True
    ) == [obj.id]
    assert await testmodels.ArrayFields.objects.filter(array_nested__item=(-1, [3, 4])).values_list(
        "id", flat=True
    ) == [obj.id]
    assert (
        await testmodels.ArrayFields.objects.filter(array_nested__item=(0, [3, 4])).values_list("id", flat=True) == []
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_item_reads_the_same_without_standard_conforming_strings(db_array_fields):
    """The row-extracting regexp_replace() pattern holds backslashes - bound, not a literal a
    standard_conforming_strings=off session would read escapes in."""
    from hare.dialects.postgresql.functions.array import ArrayItem
    from hare.transactions.transactions import Transactions

    obj = await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[1, 2], [3, 4]])
    await testmodels.ArrayFields.objects.create(array=[0], array_nested=[[9, 9], [8, 8]])
    async with Transactions.atomic() as connection:
        await connection.execute_script("SET LOCAL standard_conforming_strings = off")
        matched = await testmodels.ArrayFields.objects.filter(array_nested__item=(-1, [3, 4])).values_list(
            "id", flat=True
        )
        rows = (
            await testmodels.ArrayFields.objects.filter(id=obj.id)
            .annotate(row=ArrayItem("array_nested", 1))
            .values_list("row", flat=True)
        )
        await connection.execute_script("SET LOCAL standard_conforming_strings = on")

    assert (matched, rows) == ([obj.id], [[3, 4]])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_decodes_the_element_through_the_base_field(db_array_fields):
    import datetime

    from hare.dialects.postgresql.functions.array import ArrayItem
    from tests.utils.timezone_context import override_timezone

    moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    obj = await testmodels.ArrayFields.objects.create(
        array=[0], array_json=[{"a": 1}, [1, 2]], array_enum=[testmodels.Color.RED], array_datetime=[moment]
    )
    queryset = testmodels.ArrayFields.objects.filter(id=obj.id)

    assert await queryset.annotate(item=ArrayItem("array_json", 0)).values_list("item", flat=True) == [{"a": 1}]
    instance = await queryset.annotate(item=ArrayItem("array_json", 1)).first()
    assert instance is not None
    assert instance.item == [1, 2]
    enum_items = await queryset.annotate(item=ArrayItem("array_enum", 0)).values_list("item", flat=True)
    assert enum_items == [testmodels.Color.RED]
    assert isinstance(enum_items[0], testmodels.Color)
    with override_timezone(use_tz=True, timezone="Asia/Tokyo"):
        datetime_items = await queryset.annotate(item=ArrayItem("array_datetime", 0)).values_list("item", flat=True)
        whole_array = await queryset.values_list("array_datetime", flat=True)
    assert datetime_items == [moment]
    assert datetime_items[0].utcoffset() == whole_array[0][0].utcoffset() == datetime.timedelta(hours=9)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_none_elements_allowed_for_every_base_field(db_array_fields):
    """A Postgres array element can be NULL whatever its base type - an IntField base_field's own
    non-null validators must not reject it, the same as a TextField base_field never did."""
    obj = await testmodels.ArrayFields.objects.create(
        array=[1, None, 3], array_smallint=[None], array_enum=[None, testmodels.Color.RED], array_json=[None]
    )
    await testmodels.ArrayFields.objects.bulk_create([testmodels.ArrayFields(array=[None, 2])])
    fetched = await testmodels.ArrayFields.objects.get(id=obj.id)
    assert fetched.array == [1, None, 3]
    assert fetched.array_smallint == [None]
    assert fetched.array_enum == [None, testmodels.Color.RED]
    assert await testmodels.ArrayFields.objects.filter(array=[1, None, 3]).count() == 1
    assert await testmodels.ArrayFields.objects.filter(array__contains=[3]).count() == 1
    rows = await testmodels.ArrayFields.objects.all().order_by("id").values_list("array", flat=True)
    assert rows == [[1, None, 3], [None, 2]]
    with pytest.raises(ValidationError, match=r"array\[1\]"):
        await testmodels.ArrayFields.objects.create(array=[None, 2**40])


@pytest.mark.parametrize("bad_index", ["0", "1] OR 1=1 --", True, 1.0, 2**31 - 1, -(2**31)])
def test_array_item_rejects_an_index_that_is_not_an_in_range_int(bad_index):
    from hare.dialects.postgresql.functions.array import ArrayItem

    with pytest.raises(ValidationError, match="Array item index"):
        ArrayItem("array", bad_index)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [("0", 10), ("1] OR 1=1 --", 10), (True, 10), (2**31, 10), 5, (0, 1, 2)])
async def test_array_item_filter_rejects_a_bad_index_or_pair(db_array_fields, bad_value):
    with pytest.raises(ValidationError):
        await testmodels.ArrayFields.objects.filter(array__item=bad_value)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_accepts_the_extreme_in_range_indexes(db_array_fields):
    from hare.dialects.postgresql.functions.array import ArrayItem

    obj = await testmodels.ArrayFields.objects.create(array=[10, 20, 30])
    queryset = testmodels.ArrayFields.objects.filter(id=obj.id)
    assert await queryset.filter(array__item=(2**31 - 2, 10)).count() == 0
    assert await queryset.filter(array__item=(-(2**31 - 1), 10)).count() == 0
    assert await queryset.annotate(last=ArrayItem("array", -1)).values_list("last", flat=True) == [30]
