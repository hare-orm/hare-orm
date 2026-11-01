"""Field contract matrix, the request query column: for every field sample, a request query of its model
declares the filter of the field, describes the parameter it makes, and a request giving each of the sample's
values finds the row holding it; a field no request can filter by is refused when the query is declared."""

import importlib
from dataclasses import replace
from typing import Literal

import pytest

from hare import fields
from hare.contrib.request_query import CursorPagination, FilterField, OrderingConfig, RequestQuery
from hare.exceptions import ConfigurationError
from hare.fields.data.containers.container_field import ContainerField
from hare.query.enums import Lookup
from tests.contracts.constants import DIALECT_REQUEST_QUERIES, GENERATED_VALUE_FIELD_NAME, SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.generated_field_contract import GeneratedFieldContract
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest

REQUIRES = ("request-query",)


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation", (list[int | None], list[None], list[Literal[None]]))
async def test_a_nullable_array_element_can_be_declared_as_a_null_only_parameter(contract_database, annotation):
    sample = next(sample for sample in FIELD_SAMPLES if sample.name == "array_non_nullable")
    SampleDatabase.skip_unless_stored(sample)
    model = (await SampleDatabase.create(contract_database, sample))[-1]
    rows = await SampleDatabase.create_rows(model, replace(sample, values=([None], [1], [])))
    query_base = get_request_query_base()

    class NullElements(query_base[model]):
        value: annotation

        class Meta:
            queryset = model.objects.all()

    found = await NullElements(value=[None]).fetch()
    assert [row.id for row in found] == [rows[0].id]


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", (False, True))
@pytest.mark.parametrize(
    "sample",
    [
        sample.get_parameter()
        for sample in FIELD_SAMPLES
        if sample.values
        and not sample.requires
        and not sample.related
        and isinstance(
            sample.make_fields()[SAMPLED_FIELD_NAME],
            (ContainerField, fields.BinaryField, fields.UUIDField, fields.JSONField),
        )
    ],
)
async def test_cursor_pages_preserve_field_values_forward_and_back(contract_database, sample, generated):
    SampleDatabase.skip_unless_stored(sample)
    model = (await SampleDatabase.create(contract_database, GeneratedFieldContract.with_generated_copy(sample)))[-1]
    values = sample.values * 2
    if sample.make_fields()[SAMPLED_FIELD_NAME].null:
        values += (None,)
    await SampleDatabase.create_rows(model, replace(sample, values=values))
    field_name = GENERATED_VALUE_FIELD_NAME if generated else SAMPLED_FIELD_NAME

    class SamplePages(RequestQuery[model]):
        class Meta:
            queryset = model.objects.all()
            ordering = OrderingConfig(default=(field_name,))
            pagination = CursorPagination(default_limit=1)

    expected = await model.objects.order_by(field_name, "id").values_list("id", flat=True)
    found = []
    cursor = None
    for _ in range(len(expected) + 1):
        page = await SamplePages(cursor=cursor).page()
        found.extend(row.id for row in page.result)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert found == expected
    found = [row.id for row in page.result]
    cursor = page.previous_cursor
    for _ in range(len(expected) + 1):
        if cursor is None:
            break
        page = await SamplePages(cursor=cursor).page()
        found.extend(row.id for row in page.result)
        cursor = page.previous_cursor
    assert cursor is None
    assert found == list(reversed(expected))


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
