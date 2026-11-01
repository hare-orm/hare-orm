"""JSON aggregates reject unsupported elements through arrays and generated fields before SQL."""

import dataclasses
from copy import deepcopy

import pytest

from hare import fields
from hare.contrib.test import capture_queries
from hare.dialects.postgresql.functions.aggregates import JSONBAgg
from hare.exceptions import FieldError
from hare.query.expressions import F
from hare.query.functions import ArrayAgg
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.query_field_contract import QueryFieldContract
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample", [sample.get_parameter() for sample in FIELD_SAMPLES if sample.name in {"binary", "interval"}]
)
@pytest.mark.parametrize("depth", [0, 1, 2])
async def test_json_aggregate_refuses_non_json_elements_before_sql(contract_database, sample, depth):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("PostgreSQL JSON and array aggregates")

    def make_fields():
        field = sample.make_fields()["value"]
        for _ in range(depth):
            field = fields.ArrayField(field)
        return {
            "value": field,
            "calculated": fields.GeneratedField(F("value"), output_field=deepcopy(field)),
        }

    wrapped = dataclasses.replace(sample, name=f"{sample.name}_array_{depth}", make_fields=make_fields)
    model = (await SampleDatabase.create(contract_database, QueryFieldContract.with_composite_key(wrapped)))[-1]
    value = sample.values[0]
    for _ in range(depth):
        value = [value]
    await model.objects.create(id=1, part=2, value=value)
    for path in ("value", "calculated"):
        assert await model.objects.aggregate(result=ArrayAgg(path)) == {"result": [value]}
        async with capture_queries() as captured:
            with pytest.raises(FieldError, match="JSONBAgg"):
                await model.objects.aggregate(result=JSONBAgg(path))
            with pytest.raises(FieldError, match="JSONBAgg"):
                await model.objects.annotate(result=JSONBAgg(path)).values_list("result", flat=True)
        assert captured.count == 0
