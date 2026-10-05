"""Vector columns and similarity search on SQLite: VectorField stored as sqlite-vec's float32 BLOB,
L2Distance/CosineDistance/InnerProduct and ``__nearby`` through the sqlite-vec extension
(``load_sqlite_vec=True``). The tests needing the extension skip where the sqlite-vec package
isn't installed."""

import math
from array import array

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.vectors.sqlite_vector_extension import SqliteVectorExtension
from hare.exceptions import ConfigurationError, OperationalError, UnSupportedError, ValidationError
from hare.query.expressions import F
from hare.vectors import CosineDistance, InnerProduct, L2Distance, VectorField
from tests.dialects.sqlite.models_vector import SqliteVectorEntry

MODULES = ["tests.dialects.sqlite.models_vector"]
SQLITE_TYPES = DialectRegistry.get_dialect("sqlite").types
VECTOR_EXTENSION_INSTALLED = SqliteVectorExtension.sqlite_vec_module is not None
requires_sqlite_vec = pytest.mark.skipif(
    not VECTOR_EXTENSION_INSTALLED, reason="the sqlite-vec package isn't installed"
)


@pytest_asyncio.fixture
async def vector_db():
    async with hare_test_context(MODULES, db_url="sqlite+aiosqlite://:memory:?load_sqlite_vec=true") as context:
        yield context


@pytest_asyncio.fixture
async def plain_db():
    async with hare_test_context(MODULES, db_url="sqlite+aiosqlite://:memory:") as context:
        yield context


async def create_entries() -> tuple[SqliteVectorEntry, SqliteVectorEntry, SqliteVectorEntry]:
    first = await SqliteVectorEntry.objects.create(name="first", embedding=[1.0, 0.0, 0.0])
    second = await SqliteVectorEntry.objects.create(name="second", embedding=[0.0, 1.0, 0.0])
    third = await SqliteVectorEntry.objects.create(name="third", embedding=[1.0, 1.0, 0.0], other_embedding=[1, 1, 0])
    return first, second, third


def make_field(dimensions: int = 3) -> VectorField:
    field = VectorField(dimensions=dimensions)
    field.model_field_name = "embedding"
    return field


def test_the_column_is_a_blob_on_sqlite_and_each_dialects_own_elsewhere():
    field = make_field()
    assert field.get_column_type(DialectRegistry.get_dialect("sqlite")) == "BLOB"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "vector(3)"


@pytest.mark.parametrize("dimensions", [0, 16001, 2.5, True, "3"])
def test_dimensions_are_checked(dimensions):
    with pytest.raises(ConfigurationError, match="dimensions"):
        VectorField(dimensions=dimensions)


def test_sqlite_refuses_more_dimensions_than_sqlite_vec_takes():
    field = make_field(8193)
    with pytest.raises(UnSupportedError, match="8192"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))
    with pytest.raises(UnSupportedError, match="8192"):
        SQLITE_TYPES.get_db_value(field, [0.0] * 8193, SqliteVectorEntry)


def test_values_are_float32_blobs():
    field = make_field()
    stored = SQLITE_TYPES.get_db_value(field, [0.5, -1.25, 3], SqliteVectorEntry)
    assert stored == array("f", [0.5, -1.25, 3.0]).tobytes()
    assert SQLITE_TYPES.get_python_value(field, stored) == [0.5, -1.25, 3.0]
    assert SQLITE_TYPES.get_python_value(field, "[0.5, 2, 3]") == [0.5, 2.0, 3.0]
    assert field.to_python((1.0, 2.0, 3.0)) == [1.0, 2.0, 3.0]
    assert SQLITE_TYPES.get_db_value(field, None, SqliteVectorEntry) is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (123, "list/tuple"),
        ([1.0, 2.0], "3 dimension"),
        ([1.0, "2", 3.0], "expected a number"),
        ([1.0, math.nan, 3.0], "not a finite number"),
        ([1.0, 1e39, 3.0], "float32"),
        ([True, 1.0, 1.0], "expected a number"),
    ],
)
def test_invalid_values_are_refused(value, message):
    with pytest.raises(ValidationError, match=message):
        make_field().to_db_value(value, SqliteVectorEntry)


def test_the_inner_product_function():
    first = array("f", [1.0, 2.0, 3.0]).tobytes()
    second = array("f", [3.0, 2.0, 1.0]).tobytes()
    assert SqliteVectorExtension.get_negative_inner_product(first, second) == -10.0
    assert SqliteVectorExtension.get_negative_inner_product("[1, 2, 3]", second) == -10.0
    assert SqliteVectorExtension.get_negative_inner_product(None, second) is None
    with pytest.raises(ValueError, match="dimension mismatch"):
        SqliteVectorExtension.get_negative_inner_product(first, array("f", [1.0]).tobytes())


@pytest.mark.asyncio
async def test_vectors_are_stored_and_read_without_the_extension(plain_db):
    first, *_ = await create_entries()
    loaded = await SqliteVectorEntry.objects.get(id=first.id)
    assert loaded.embedding == [1.0, 0.0, 0.0]
    assert loaded.other_embedding is None
    assert await SqliteVectorEntry.objects.filter(embedding=[1.0, 0.0, 0.0]).count() == 1


@pytest.mark.asyncio
async def test_distances_without_the_extension_are_refused_before_sql(plain_db):
    await create_entries()
    connection = SqliteVectorEntry.get_connection()
    assert not connection.features.supports_vector_search
    with pytest.raises(UnSupportedError, match="supports_vector_search"):
        await SqliteVectorEntry.objects.annotate(distance=L2Distance("embedding", [1.0, 0.0, 0.0]))
    with pytest.raises(UnSupportedError, match="supports_vector_search"):
        await SqliteVectorEntry.objects.filter(embedding__nearby=([1.0, 0.0, 0.0], 1.0))


def test_load_sqlite_vec_is_checked():
    with pytest.raises(ConfigurationError, match="load_sqlite_vec"):
        AiosqliteClient(file_path=":memory:", connection_alias="vector_option", load_sqlite_vec="maybe")
    original_module = SqliteVectorExtension.sqlite_vec_module
    SqliteVectorExtension.sqlite_vec_module = None
    try:
        with pytest.raises(ConfigurationError, match=r"hare-orm\[sqlite-vec\]"):
            AiosqliteClient(file_path=":memory:", connection_alias="vector_option", load_sqlite_vec=True)
    finally:
        SqliteVectorExtension.sqlite_vec_module = original_module


@requires_sqlite_vec
@pytest.mark.asyncio
async def test_distances_order_the_nearest_first(vector_db):
    first, second, third = await create_entries()
    assert SqliteVectorEntry.get_connection().features.supports_vector_search
    nearest = await (
        SqliteVectorEntry.objects.annotate(distance=L2Distance("embedding", [0.9, 0.1, 0.0]))
        .order_by("distance")
        .limit(2)
        .values_list("name", "distance")
    )
    assert [name for name, _ in nearest] == ["first", "third"]
    assert nearest[0][1] == pytest.approx(math.sqrt(0.02), rel=1e-5)
    cosine = dict(
        await SqliteVectorEntry.objects.annotate(distance=CosineDistance("embedding", [1.0, 1.0, 0.0])).values_list(
            "name", "distance"
        )
    )
    assert cosine["third"] == pytest.approx(0.0, abs=1e-6)
    assert cosine["first"] == pytest.approx(1 - 1 / math.sqrt(2), rel=1e-5)
    inner = dict(
        await SqliteVectorEntry.objects.annotate(distance=InnerProduct("embedding", [2.0, 3.0, 0.0])).values_list(
            "name", "distance"
        )
    )
    assert inner == {"first": -2.0, "second": -3.0, "third": -5.0}


@requires_sqlite_vec
@pytest.mark.asyncio
async def test_a_distance_between_two_columns_and_in_filters(vector_db):
    *_, third = await create_entries()
    same = await SqliteVectorEntry.objects.annotate(
        distance=L2Distance("embedding", F("other_embedding"))
    ).values_list("name", "distance")
    assert dict(same)["third"] == 0.0
    assert dict(same)["first"] is None
    close = await (
        SqliteVectorEntry.objects.annotate(distance=CosineDistance("embedding", [1.0, 0.0, 0.0]))
        .filter(distance__lt=0.5)
        .order_by("name")
        .values_list("name", flat=True)
    )
    assert close == ["first", "third"]
    assert await SqliteVectorEntry.objects.filter(embedding__nearby=([1.0, 0.0, 0.0], 1.0)).order_by(
        "name"
    ).values_list("name", flat=True) == ["first", "third"]


@requires_sqlite_vec
@pytest.mark.asyncio
async def test_a_plan_binds_each_query_vector(vector_db):
    await create_entries()
    for query_vector, expected_name in (([1.0, 0.0, 0.0], "first"), ([0.0, 1.0, 0.0], "second")):
        nearest = await (
            SqliteVectorEntry.objects.annotate(distance=InnerProduct("embedding", query_vector))
            .order_by("distance", "id")
            .first()
        )
        assert nearest is not None
        assert nearest.name == expected_name


@requires_sqlite_vec
@pytest.mark.asyncio
async def test_query_vectors_are_checked(vector_db):
    await create_entries()
    with pytest.raises(ValidationError, match="3 dimension"):
        await SqliteVectorEntry.objects.annotate(distance=L2Distance("embedding", [1.0, 0.0]))
    with pytest.raises(ValidationError, match="distance must be a finite number"):
        await SqliteVectorEntry.objects.filter(embedding__nearby=([1.0, 0.0, 0.0], "far"))
    with pytest.raises(OperationalError, match="Error reading 2nd vector"):
        await SqliteVectorEntry.objects.annotate(distance=L2Distance(F("embedding"), F("name")))
