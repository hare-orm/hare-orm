"""JSONBAgg() of a JSONField with a declared field_type: each element is the declared type, as reading the
column gives it."""

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.dialects.postgresql.functions.aggregates import JSONBAgg
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.queries.models_jsonb_documents import JsonbDocument, JsonbDocumentRow

REQUIRES = ("pydantic",)


@pytest.mark.asyncio
async def test_jsonb_agg_gives_each_element_the_declared_type():
    """Each element came back a dict - the declared type was applied to no element of a JSONField."""
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.dialects.postgresql.queries.models_jsonb_documents"],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
    ):
        await JsonbDocumentRow.objects.create(id=1, document={"title": "hello"})
        await JsonbDocumentRow.objects.create(id=2, document=None)
        result = await JsonbDocumentRow.objects.aggregate(documents=JSONBAgg("document", order_by="id"))
        assert result["documents"] == [JsonbDocument(title="hello"), None]
