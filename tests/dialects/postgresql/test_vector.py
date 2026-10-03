"""Tests for VectorField/L2Distance/CosineDistance/InnerProduct (hare.dialects.postgresql.vector).

Uses the db_vector fixture (tests/dialects/postgresql/conftest.py), which manually creates the
vector extension and runs generate_schemas() against tests.dialects.postgresql.models_vector - same
pattern as db_postgis for PostGISField.
"""

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.functions.vector import CosineDistance, InnerProduct, L2Distance
from hare.dialects.postgresql.indexes import HnswIndex, IvfflatIndex
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from tests.dialects.postgresql.models_vector import VectorEntry


def test_sql_type_raises_unsupported_error_for_non_postgres_dialect():
    """vector(N) is a pgvector/Postgres-only column type - resolving it for another dialect's
    DDL must raise a clear ConfigurationError instead of silently handing back Postgres syntax
    for schema generation to choke on."""
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    with pytest.raises(UnSupportedError, match="VectorField.*embedding.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


def test_sql_type_still_resolves_for_postgres_dialect():
    field = VectorField(dimensions=1536)
    field.model_field_name = "embedding"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "vector(1536)"


def test_to_db_value_rejects_wrong_shape():
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    with pytest.raises(ValidationError):
        field.to_db_value(123, VectorEntry)


def test_to_db_value_rejects_wrong_dimension_count():
    field = VectorField(dimensions=5)
    field.model_field_name = "embedding"
    with pytest.raises(ValidationError, match="embedding.*5.*3"):
        field.to_db_value([0.1, 0.2, 0.3], VectorEntry)


def test_to_db_value_accepts_matching_dimension_count():
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    assert field.to_db_value([0.1, 0.2, 0.3], VectorEntry) == "[0.1,0.2,0.3]"


def test_to_db_value_formats_bracketed_text():
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    assert field.to_db_value([0.5, -1.25, 3.0], VectorEntry) == "[0.5,-1.25,3.0]"
    assert field.to_db_value(None, VectorEntry) is None


def test_to_python_value_parses_bracketed_text():
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    assert field.from_db_value("[0.5,-1.25,3.0]") == [0.5, -1.25, 3.0]
    assert field.from_db_value(None) is None


def test_format_and_parse_vector_text_round_trip():
    values = [0.5, -1.25, 3.0]
    text = VectorField.format_vector_text(values)
    assert text == "[0.5,-1.25,3.0]"
    assert VectorField.parse_vector_text(text) == values


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_vector_round_trip(db_vector):
    entry = await VectorEntry.objects.create(name="a", embedding=[0.1, -0.2, 0.3])
    reread = await VectorEntry.objects.get(id=entry.id)
    assert reread.embedding == pytest.approx([0.1, -0.2, 0.3])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_vector_round_trip_with_round_valued_vectors(db_vector):
    """Every byte of a "round" float4 like 2.0/0.0's big-endian binary representation happens to
    be < 0x80, so it passes as valid UTF-8 by accident - a driver that decides between text and
    hex decoding by checking UTF-8 validity mishandles exactly this shape of value, even though
    test_vector_round_trip's [0.1, -0.2, 0.3] (whose bytes are NOT valid UTF-8) round-trips fine.
    """
    round_valued = await VectorEntry.objects.create(name="round", embedding=[2.0, 0.0, 0.0])
    all_zero = await VectorEntry.objects.create(name="zero", embedding=[0.0, 0.0, 0.0])
    mixed = await VectorEntry.objects.create(name="mixed", embedding=[1.0, -1.0, 64.0])

    assert (await VectorEntry.objects.get(id=round_valued.id)).embedding == pytest.approx([2.0, 0.0, 0.0])
    assert (await VectorEntry.objects.get(id=all_zero.id)).embedding == pytest.approx([0.0, 0.0, 0.0])
    assert (await VectorEntry.objects.get(id=mixed.id)).embedding == pytest.approx([1.0, -1.0, 64.0])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_l2_distance_orders_by_nearest(db_vector):
    near = await VectorEntry.objects.create(name="near", embedding=[1.0, 0.0, 0.0])
    far = await VectorEntry.objects.create(name="far", embedding=[0.0, 0.0, 10.0])

    rows = (
        await VectorEntry.objects.all()
        .annotate(dist=L2Distance("embedding", [1.0, 0.0, 0.0]))
        .order_by("dist")
        .values("id", "dist")
    )
    assert rows[0]["id"] == near.id
    assert rows[0]["dist"] == pytest.approx(0.0)
    assert rows[-1]["id"] == far.id
    assert rows[-1]["dist"] > rows[0]["dist"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_cosine_distance_orders_by_nearest(db_vector):
    same_direction = await VectorEntry.objects.create(name="same", embedding=[2.0, 0.0, 0.0])
    opposite = await VectorEntry.objects.create(name="opposite", embedding=[-1.0, 0.0, 0.0])

    rows = (
        await VectorEntry.objects.all()
        .annotate(dist=CosineDistance("embedding", [1.0, 0.0, 0.0]))
        .order_by("dist")
        .values("id", "dist")
    )
    assert rows[0]["id"] == same_direction.id
    assert rows[0]["dist"] == pytest.approx(0.0, abs=1e-6)
    assert rows[-1]["id"] == opposite.id
    assert rows[-1]["dist"] == pytest.approx(2.0, abs=1e-6)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inner_product_orders_by_nearest(db_vector):
    """pgvector's <#> is the NEGATIVE inner product, so the entry with the LARGEST plain dot
    product against the query vector sorts FIRST (most negative) with a plain order_by("dist")."""
    largest_dot = await VectorEntry.objects.create(name="largest", embedding=[5.0, 0.0, 0.0])
    smallest_dot = await VectorEntry.objects.create(name="smallest", embedding=[1.0, 0.0, 0.0])

    rows = (
        await VectorEntry.objects.all()
        .annotate(dist=InnerProduct("embedding", [1.0, 0.0, 0.0]))
        .order_by("dist")
        .values("id", "dist")
    )
    assert rows[0]["id"] == largest_dot.id
    assert rows[0]["dist"] == pytest.approx(-5.0, abs=1e-6)
    assert rows[-1]["id"] == smallest_dot.id
    assert rows[-1]["dist"] == pytest.approx(-1.0, abs=1e-6)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nearby_lookup_matches_l2_distance_filter(db_vector):
    near = await VectorEntry.objects.create(name="near", embedding=[1.0, 0.0, 0.0])
    await VectorEntry.objects.create(name="far", embedding=[0.0, 0.0, 10.0])

    via_lookup = await VectorEntry.objects.filter(embedding__nearby=([1.0, 0.0, 0.0], 1.0))
    via_annotate = (
        await VectorEntry.objects.all().annotate(dist=L2Distance("embedding", [1.0, 0.0, 0.0])).filter(dist__lte=1.0)
    )
    assert [e.id for e in via_lookup] == [near.id]
    assert [e.id for e in via_lookup] == [e.id for e in via_annotate]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nearby_lookup_rejects_wrong_dimension_query_vector(db_vector):
    """__nearby's own value_encoder used to skip field_object.to_db_value()/validate() entirely
    - a wrong-dimension query_vector reached Postgres as an unchecked literal, raising a raw
    driver error instead of the framework's own clean ValidationError (the same check create()/
    save() already run for this exact field)."""
    with pytest.raises(ValidationError, match="embedding.*3.*2"):
        await VectorEntry.objects.filter(embedding__nearby=([1.0, 0.0], 1.0))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_l2_distance_rejects_wrong_dimension_query_vector(db_vector):
    """VectorDistanceExpression._get_vector_operand() used to build the pgvector literal
    straight from the raw query vector, skipping the target field's own validate() - same bug
    class as __nearby above."""
    with pytest.raises(ValidationError, match="embedding.*3.*2"):
        await VectorEntry.objects.all().annotate(dist=L2Distance("embedding", [1.0, 0.0])).values("dist")


@pytest.mark.parametrize(
    "bad_vector", [["a", 1.0, 2.0], [float("nan"), 1.0, 2.0], [float("inf"), 1.0, 2.0], [None, 1, 2]]
)
def test_to_db_value_rejects_non_finite_or_non_numeric_elements(bad_vector):
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    with pytest.raises(ValidationError, match=r"embedding\[0\]"):
        field.to_db_value(bad_vector, VectorEntry)


@pytest.mark.parametrize("out_of_range", [1e39, -3.5e38])
def test_to_db_value_rejects_elements_outside_the_float4_range(out_of_range):
    field = VectorField(dimensions=3)
    field.model_field_name = "embedding"
    with pytest.raises(ValidationError, match=r"embedding\[1\].*float4"):
        field.to_db_value([0.0, out_of_range, 0.0], VectorEntry)
    assert field.to_db_value([0.0, 3.4028234663852886e38, 0.0], VectorEntry) == "[0.0,3.4028234663852886e+38,0.0]"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_vector_reads_back_exactly_the_written_decimal_values(db_vector):
    """rust_pg read each float4 element as its raw double (0.10000000149011612) - both drivers
    now return the shortest decimal, so a read-back value equals the written one."""
    entry = await VectorEntry.objects.create(name="a", embedding=[0.1, 0.2, 0.3])

    reread = await VectorEntry.objects.get(id=entry.id)

    assert reread.embedding == [0.1, 0.2, 0.3]
    assert await VectorEntry.objects.filter(embedding=reread.embedding).values_list("id", flat=True) == [entry.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_vector_in_and_not_in_with_many_values(db_vector):
    """20+ values go through `= ANY(CAST($1 AS vector(3)[]))` - rust_pg couldn't bind a list of
    vector strings as an array parameter."""
    near = await VectorEntry.objects.create(name="near", embedding=[1.0, 2.0, 3.0])
    far = await VectorEntry.objects.create(name="far", embedding=[0.5, 0.5, 0.5])
    candidates = [[float(index), 0.0, 0.0] for index in range(25)] + [[1.0, 2.0, 3.0]]

    assert await VectorEntry.objects.filter(embedding__in=candidates).values_list("id", flat=True) == [near.id]
    assert await VectorEntry.objects.filter(embedding__not_in=candidates).values_list("id", flat=True) == [far.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_agg_of_a_vector_field(db_vector):
    """rust_pg returned an ARRAY_AGG of vectors as one hex blob."""
    from hare.dialects.postgresql.functions.aggregates import ArrayAgg

    await VectorEntry.objects.create(name="a", embedding=[0.1, 0.2, 0.3])
    await VectorEntry.objects.create(name="b", embedding=[1.0, 2.0, 3.0])

    aggregated = await VectorEntry.objects.all().aggregate(embeddings=ArrayAgg("embedding"))

    assert sorted(aggregated["embeddings"]) == [[0.1, 0.2, 0.3], [1.0, 2.0, 3.0]]


def test_hnsw_index_over_fields_requires_opclasses():
    """pgvector has no default HNSW operator class - an HnswIndex(fields=...) without opclasses=
    was accepted and only failed at `migrate`, with "data type vector has no default operator
    class for access method hnsw"."""
    with pytest.raises(ConfigurationError, match="HnswIndex requires opclasses"):
        HnswIndex(fields=("embedding",))

    index = HnswIndex(fields=("embedding",), opclasses=("vector_cosine_ops",))

    assert index.opclasses == ["vector_cosine_ops"]


def test_ivfflat_index_over_fields_keeps_the_default_opclass():
    assert IvfflatIndex(fields=("embedding",)).opclasses == []


@pytest.mark.parametrize("lists", [0, -1, 32769, True, 4.0, "4); DROP TABLE vector_entry; --"], ids=repr)
def test_ivfflat_index_rejects_lists_outside_pgvector_range_or_not_int(lists):
    """`lists` is written into the DDL verbatim - a non-int (a bool, a SQL fragment) or a value
    pgvector rejects is refused up front."""
    with pytest.raises(ConfigurationError, match="IvfflatIndex lists"):
        IvfflatIndex(fields=("embedding",), lists=lists)


def test_ivfflat_index_accepts_lists_at_pgvector_bounds():
    assert IvfflatIndex(fields=("embedding",), lists=1).lists == 1
    assert IvfflatIndex(fields=("embedding",), lists=32768).lists == 32768


@pytest.mark.parametrize(
    ("m", "ef_construction", "message"),
    [
        (1, 64, "HnswIndex m"),
        (101, 256, "HnswIndex m"),
        (True, 64, "HnswIndex m"),
        ("16", 64, "HnswIndex m"),
        (16, 3, "HnswIndex ef_construction"),
        (16, 1001, "HnswIndex ef_construction"),
        (16, False, "HnswIndex ef_construction"),
        (16, "64) ; --", "HnswIndex ef_construction"),
        (16, 31, r"at least 2 \* m"),
    ],
)
def test_hnsw_index_rejects_parameters_outside_pgvector_range_or_not_int(m, ef_construction, message):
    with pytest.raises(ConfigurationError, match=message):
        HnswIndex(fields=("embedding",), opclasses=("vector_l2_ops",), m=m, ef_construction=ef_construction)


def test_hnsw_index_accepts_parameters_at_pgvector_bounds():
    index = HnswIndex(fields=("embedding",), opclasses=("vector_l2_ops",), m=2, ef_construction=4)
    assert (index.m, index.ef_construction) == (2, 4)
    index = HnswIndex(fields=("embedding",), opclasses=("vector_l2_ops",), m=100, ef_construction=1000)
    assert (index.m, index.ef_construction) == (100, 1000)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_unnamed_indexes_on_one_column_get_distinct_names(db_vector):
    """Two HNSW indexes with different operator classes and an IVFFlat one on the same column
    used to share one generated name - schema creation failed with "relation already exists"."""
    rows = await db_vector.db().execute_dicts(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'vector_entry' AND indexdef LIKE '%embedding%'"
    )

    index_definitions = sorted(row["indexdef"] for row in rows)
    assert len(index_definitions) == 3
    assert any("hnsw" in definition and "vector_cosine_ops" in definition for definition in index_definitions)
    assert any("hnsw" in definition and "vector_l2_ops" in definition for definition in index_definitions)
    assert any("ivfflat" in definition for definition in index_definitions)
