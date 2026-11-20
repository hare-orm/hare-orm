"""PostgreSQL full-text configuration accepts text expressions as well as literals."""

import sys
from types import ModuleType

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.query.expressions import F, Value
from hare.query.functions import Coalesce
from hare.search import SearchHeadline, SearchQuery, SearchVector
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_search_config_expressions_follow_each_rows_configuration(monkeypatch):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("regconfig is a PostgreSQL configuration type")

    class SearchRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        body = fields.TextField()
        configuration = fields.CharField(max_length=20)

    module = ModuleType("tests._search_config_expressions")
    module.__models__ = [SearchRecord]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    expected = {
        "simple": ("'cat':1 'cats':2 'rat':3", "'cats'", "cat [cats] rat"),
        "english": ("'cat':1,2 'rat':3", "'cat'", "[cat] [cats] rat"),
    }
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        await SearchRecord.objects.bulk_create(
            [
                SearchRecord(id=index, body="cat cats rat", configuration=config)
                for index, config in enumerate(expected, 1)
            ]
        )
        for config in ("simple", Value("simple"), F("configuration"), Coalesce("configuration", "simple")):
            row_configs = ("simple", "simple") if isinstance(config, (str, Value)) else ("simple", "english")
            for repeat in range(2):
                rows = (
                    await SearchRecord.objects.order_by("id")
                    .annotate(
                        vector=SearchVector("body", config=config),
                        query=SearchQuery("cats", config=config),
                        headline=SearchHeadline(
                            "body",
                            SearchQuery("cats", config=config),
                            config=config,
                            start_sel="[",
                            stop_sel="]",
                            highlight_all=True,
                        ),
                    )
                    .values_list("vector", "query", "headline")
                )
                assert rows == [expected[name] for name in row_configs], repeat
