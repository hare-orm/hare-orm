"""Field contract matrix, the request query column: for every field sample, a request query of its model
declares the filter of the field, describes the parameter it makes, and a request giving each of the sample's
values finds the row holding it; a field no request can filter by is refused when the query is declared."""

import importlib

import pytest

from hare.contrib.request_query import FilterField, RequestQuery
from hare.exceptions import ConfigurationError
from hare.query.enums import Lookup
from tests.contracts.constants import DIALECT_REQUEST_QUERIES, SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest

REQUIRES = ("request-query",)


def get_request_query_base() -> type[RequestQuery]:
    """The request query class of the database under test's dialect, the base one when it has none."""
    location = DIALECT_REQUEST_QUERIES.get(DatabaseUnderTest.get_dialect().name)
    if location is None:
        return RequestQuery
    return getattr(importlib.import_module(location[0]), location[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [
        sample.get_parameter()
        for sample in FIELD_SAMPLES
        if sample.values and sample.registered_live and not sample.requires and sample.request_filtered
    ],
)
async def test_a_request_filtering_by_a_field_finds_the_row_holding_the_value(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    models = await SampleDatabase.create(contract_database, sample)
    row_model = models[-1]
    rows = await SampleDatabase.create_rows(row_model, sample)
    filters = (FilterField(SAMPLED_FIELD_NAME, lookups=(Lookup.EXACT,)),)

    if sample.request_filter_refused:
        with pytest.raises(ConfigurationError, match="takes any value"):
            get_request_query_base().for_model(row_model, filters=filters)
        return
    query_class = get_request_query_base().for_model(row_model, filters=filters)
    descriptions = {description.name: description for description in query_class.describe_parameters()}
    assert descriptions[SAMPLED_FIELD_NAME].lookup == Lookup.EXACT
    request_values = sample.request_values if sample.request_values is not None else sample.values
    for row, value in zip(rows, request_values, strict=True):
        found = await query_class(**{SAMPLED_FIELD_NAME: value}).fetch()
        assert row.pk in [found_row.pk for found_row in found], value
