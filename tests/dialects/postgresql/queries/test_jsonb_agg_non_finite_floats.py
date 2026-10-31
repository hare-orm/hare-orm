"""JSONBAgg() of a float field: PostgreSQL's jsonb holds a non-finite float as text, which is read back as
the float - in an array of floats too - and a text field's same text stays text."""

import math
import os

import pytest

from hare.contrib.test import hare_test_context
from hare.dialects.postgresql.functions.aggregates import JSONBAgg
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.queries.models_jsonb_numbers import JsonbNumberRow


@pytest.mark.asyncio
async def test_jsonb_agg_reads_the_non_finite_floats_back():
    """A non-finite float came back as its text, which FloatField refused - the aggregate raised."""
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.dialects.postgresql.queries.models_jsonb_numbers"],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
    ):
        await JsonbNumberRow.objects.create(
            id=1, amount=float("inf"), numbers=[float("nan"), float("-inf")], label="NaN"
        )
        result = await JsonbNumberRow.objects.aggregate(
            amounts=JSONBAgg("amount"), numbers=JSONBAgg("numbers"), labels=JSONBAgg("label")
        )
        assert result["amounts"] == [float("inf")]
        assert math.isnan(result["numbers"][0][0])
        assert result["numbers"][0][1] == float("-inf")
        assert result["labels"] == ["NaN"]
