"""Ranges and arrays of a rust.native.pg result read straight from the values the driver read (a
PgResult) against the same reader on the result's PgRow objects."""

import datetime
from decimal import Decimal

import pytest

from hare.dialects.postgresql.fields.ranges import Range
from tests.dialects.postgresql.models_agg import AggAuthor, AggPeriod
from tests.utils.pg_result_reading import PgResultReading
from tests.utils.timezone_context import override_timezone

MOMENT = datetime.datetime(2024, 3, 10, 6, 59, 59, 999999, tzinfo=datetime.UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "zone"), [(True, "UTC"), (True, "Europe/Berlin"), (False, "UTC")])
async def test_ranges_and_arrays(db_agg, use_timezone, zone):
    with override_timezone(use_timezone, zone):
        author = await AggAuthor.objects.create(name="a")
        await PgResultReading.assert_result_read_as_rows(
            AggPeriod,
            [
                {
                    "author": author,
                    "span": Range(1, 10),
                    "decimal_span": Range(Decimal("1.5"), Decimal("2.25"), upper_inc=True),
                    "date_span": Range(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),
                    "moment_span": Range(MOMENT, MOMENT + datetime.timedelta(days=1)),
                    "tags": ["x", "", "y z"],
                    "matrix": [[1, 2], [3, 4]],
                },
                {
                    "author": author,
                    "span": Range(None, 5, lower_inc=False),
                    "decimal_span": Range(is_empty=True),
                    "date_span": Range(datetime.date(2024, 1, 1), None),
                    "moment_span": Range(None, MOMENT),
                    "tags": [],
                    "matrix": None,
                },
                {
                    "author": author,
                    "span": Range(3, 3),
                    "decimal_span": None,
                    "date_span": Range(is_empty=True),
                    "moment_span": None,
                    "tags": None,
                    "matrix": [[5]],
                },
            ],
        )
