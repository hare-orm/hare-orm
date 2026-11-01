"""Field values across paging, windows, nullable results and RETURNING, with Python expectations."""

import datetime
import math

import pytest

from hare.contrib.test import capture_queries
from hare.dialects.clickhouse.fields import ClickhouseDatetimeField
from hare.dialects.postgresql.functions.array import ArraySubquery
from hare.exceptions import UnSupportedError
from hare.query.expressions import F, Window
from hare.query.functions import Cast, NullIf
from hare.query.functions.window import Lag
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.query_field_contract import QueryFieldContract
from tests.contracts.sample_database import SampleDatabase


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [
        sample.get_parameter()
        for sample in FIELD_SAMPLES
        if sample.name
        in {
            "int",
            "float",
            "decimal",
            "char_enum",
            "uuid",
            "datetime",
            "binary",
            "array",
            "array_non_nullable",
            "tuple",
            "map",
            "nested",
            "dynamic",
            "variant",
            "float32",
            "int256",
        }
    ],
)
async def test_field_values_survive_query_paths(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    model = (await SampleDatabase.create(contract_database, QueryFieldContract.with_composite_key(sample)))[-1]
    values = [*sample.values, *sample.values]
    await model.objects.bulk_create(
        [model(id=position // 2, part=position % 2, value=value) for position, value in enumerate(values)]
    )
    expected = QueryFieldContract.comparable(values)
    query = model.objects.order_by("id", "part").annotate(result=F("value"))
    for shape in ("model", "dict", "flat"):
        for iterate in (False, True):
            actual = await QueryFieldContract.read(query, shape, iterate=iterate)
            assert QueryFieldContract.comparable(actual) == expected, (shape, iterate)
    for position in range(len(values)):
        boundary = (position // 2, position % 2)
        after = await query.after_cursor(*boundary).values_list("result", flat=True)
        before = await query.before_cursor(*boundary).values_list("result", flat=True)
        assert QueryFieldContract.comparable(after) == expected[position + 1 :]
        assert QueryFieldContract.comparable(before) == expected[:position]

    for offset in (1, 0, 2, 1):
        window = model.objects.order_by("id", "part").annotate(
            result=Window(Lag("value", offset=offset, default=values[0]), order_by=("id", "part"))
        )
        neighbors = [values[position - offset] if position >= offset else values[0] for position in range(len(values))]
        for shape in ("model", "flat"):
            actual = await QueryFieldContract.read(window, shape, iterate=True)
            assert QueryFieldContract.comparable(actual) == QueryFieldContract.comparable(neighbors), (offset, shape)

    connection = model._meta.connection
    null_query = model.objects.annotate(result=NullIf("value", F("value"))).order_by("id", "part")
    if connection.dialect.name == "clickhouse" and sample.name in {
        "array",
        "array_non_nullable",
        "tuple",
        "map",
        "nested",
        "dynamic",
        "variant",
    }:
        async with capture_queries() as counter:
            with pytest.raises(UnSupportedError, match="NullIf"):
                await null_query
        assert counter.count == 0
    else:
        # Native NaN equality differs by database; finite values retain the NULLIF guarantee.
        nulls = await null_query.filter(part=0).values_list("result", flat=True)
        assert nulls == [None] * len(values[::2])

    previous_marker = 0
    for marker, shape in enumerate(("dict", "model", "old"), 1):
        write = model.objects.all().update(marker=marker)
        if shape == "model":
            write = write.returning()
        else:
            write = write.returning(
                "pk", "value", "marker", **({"old": ("value", "marker")} if shape == "old" else {})
            )
        if not connection.features.supports_returning or (
            shape == "old" and not connection.features.supports_returning_old_new
        ):
            async with capture_queries() as counter:
                with pytest.raises(UnSupportedError):
                    await write
            assert counter.count == 0
            continue
        rows = sorted(await write, key=lambda row: row.pk if shape == "model" else row["pk"])
        returned = [row.value if shape == "model" else row["value"] for row in rows]
        assert QueryFieldContract.comparable(returned) == expected
        assert [row.marker if shape == "model" else row["marker"] for row in rows] == [marker] * len(values)
        if shape == "old":
            assert QueryFieldContract.comparable([row["old"]["value"] for row in rows]) == expected
            assert [row["old"]["marker"] for row in rows] == [previous_marker] * len(values)
        previous_marker = marker


@pytest.mark.asyncio
async def test_native_annotations_use_the_iteration_connection(contract_database):
    dialect = contract_database.get_connection().dialect.name
    if dialect not in {"postgresql", "clickhouse"}:
        pytest.skip("the expressions require PostgreSQL or ClickHouse")
    sample = next(sample for sample in FIELD_SAMPLES if sample.name == "datetime")
    model = (await SampleDatabase.create(contract_database, QueryFieldContract.with_composite_key(sample)))[-1]
    moment = datetime.datetime(2024, 1, 1, microsecond=123456, tzinfo=datetime.UTC)
    await model.objects.bulk_create([model(id=1, part=1, value=moment), model(id=1, part=2, value=None)])
    if dialect == "postgresql":
        expression = ArraySubquery(model.objects.order_by("id", "part").values("part"))
        expected = [[1, 2], [1, 2]]
    else:
        expression = Cast("value", ClickhouseDatetimeField(precision=0, timezone="UTC", null=True))
        expected = [moment.replace(microsecond=0), None]
    query = model.objects.order_by("id", "part").annotate(result=expression)
    for shape in ("model", "dict", "flat"):
        for pinned in (False, True):
            source = query.using(model._meta.connection.connection_alias) if pinned else query
            assert await QueryFieldContract.read(source, shape, iterate=True) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_name", ["float", "float32"])
async def test_field_cursor_boundaries_use_the_stored_values(contract_database, sample_name):
    sample = next(sample for sample in FIELD_SAMPLES if sample.name == sample_name)
    SampleDatabase.skip_unless_stored(sample)
    model = (await SampleDatabase.create(contract_database, QueryFieldContract.with_composite_key(sample)))[-1]
    dialect = contract_database.get_connection().dialect.name
    values = [0.1, 0.1, 0.2, None]
    if sample_name == "float" and dialect != "sqlite":
        values = [1.5, float("nan"), None, float("nan"), -2.0]
    records = [(position // 2, position % 2, value) for position, value in enumerate(values)]
    await model.objects.bulk_create([model(id=first, part=second, value=value) for first, second, value in records])
    for descending in (False, True):
        for nulls_option in (None, False, True):
            nulls_first = (
                (not descending if dialect == "sqlite" else descending) if nulls_option is None else nulls_option
            )

            def ordering_key(record):
                first, second, value = record
                if value is None:
                    return (0 if nulls_first else 3, 0, first, second)
                nan_first = nulls_first if dialect == "clickhouse" else descending
                if math.isnan(value):
                    return (1 if nan_first else 2, 0, first, second)
                return (2 if nan_first else 1, -value if descending else value, first, second)

            expected = sorted(records, key=ordering_key)
            ordering = F("value").desc if descending else F("value").asc
            expression = (
                ordering() if nulls_option is None else ordering(nulls_first=nulls_first, nulls_last=not nulls_first)
            )
            query = model.objects.order_by(expression, "id", "part")
            expected_keys = [(first, second) for first, second, value in expected]
            iterated = []
            async for row in query.iterator(chunk_size=1):
                iterated.append(row.pk)
                if len(iterated) > len(expected):
                    break
            assert iterated == expected_keys
            for position in {0, len(expected) // 2, len(expected) - 1}:
                first, second, value = expected[position]
                after = await query.after_cursor(value, first, second)
                before = await query.before_cursor(value, first, second)
                assert [row.pk for row in after] == expected_keys[position + 1 :]
                assert [row.pk for row in before] == expected_keys[:position]
                assert [row.pk for row in await query.after_cursor(value, first, second).limit(1)] == expected_keys[
                    position + 1 : position + 2
                ]
                assert [row.pk for row in await query.before_cursor(value, first, second).limit(1)] == expected_keys[
                    max(0, position - 1) : position
                ]
