"""pg_trgm lookups and similarity/distance functions, and the unaccent transform."""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context, truncate_all_models
from hare.dialects.postgresql.functions.trigram import (
    TrigramDistance,
    TrigramSimilarity,
    TrigramStrictWordDistance,
    TrigramStrictWordSimilarity,
    TrigramWordDistance,
    TrigramWordSimilarity,
)
from hare.exceptions import FieldError
from hare.query.expressions import F, Q
from hare.query.functions import Concat, Upper
from hare.query.plans.statement_plans import StatementPlans
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.typed_row_models import TypedRow, TypedRowNote


@pytest_asyncio.fixture(scope="module")
async def text_context() -> AsyncGenerator[Any]:
    skip_if_not_postgres()
    db_url = os.environ["HARE_TEST_DB"].replace("\\{", "{").replace("\\}", "}")
    db_url = db_url.format(uuid.uuid4().hex) if "{}" in db_url else db_url
    async with hare_test_context(["tests.typed_row_models"], db_url=db_url) as ctx:
        await ctx.db().execute_script(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent"
        )
        yield ctx


@pytest_asyncio.fixture
async def names(text_context: Any) -> AsyncGenerator[None]:
    for row_id, name in enumerate(["Gérard Depardieu", "Gerhard Schröder", "Zoë Saldaña", "word"], 1):
        await TypedRow.objects.create(id=row_id, name=name, body=name.upper())
    await TypedRowNote.objects.create(id=10, row_id=1)
    yield
    await truncate_all_models()


async def matching(*conditions, **lookups):
    return sorted(await TypedRow.objects.filter(*conditions, **lookups).values_list("id", flat=True))


@pytest.mark.asyncio
async def test_trigram_lookups(names):
    assert await matching(name__trigram_similar="Gerard Depardeu") == [1]
    assert await matching(name__trigram_word_similar="Gerhard") == [2]
    assert await matching(name__trigram_strict_word_similar="Depard") == [1]


@pytest.mark.asyncio
async def test_trigram_lookups_of_annotations_and_relations(names):
    shouted = TypedRow.objects.annotate(shouted=Upper("name"))
    assert await shouted.filter(shouted__trigram_similar="GERARD DEPARDEU").values_list("id", flat=True) == [1]
    joined = TypedRow.objects.annotate(both=Concat("name", "body"))
    assert await joined.filter(both__trigram_word_similar="Gerhard").values_list("id", flat=True) == [2]
    assert await TypedRowNote.objects.filter(row__name__trigram_similar="Gerard Depardeu").values_list(
        "id", flat=True
    ) == [10]
    described = shouted.get_lookup_info("shouted__trigram_similar")
    assert (described.requires_extension, described.dialects, described.value_type) == (
        "pg_trgm",
        frozenset({"postgresql"}),
        str,
    )
    of_field = TypedRow._meta.get_lookup_info("name__trigram_similar")
    assert (of_field.requires_extension, of_field.dialects) == ("pg_trgm", frozenset({"postgresql"}))


@pytest.mark.asyncio
async def test_unaccent_transform(names):
    assert await matching(name__unaccent="Gerard Depardieu") == [1]
    assert await matching(name__unaccent__icontains="SALDANA") == [3]
    assert await matching(name__unaccent__startswith="Zoe") == [3]
    assert await matching(name__unaccent__in=["Zoe Saldana", "x"]) == [3]
    assert await matching(body__unaccent__contains="ZOE") == [3]
    assert await matching(name__unaccent__trigram_similar="Gerard Depardeu") == [1]
    assert await matching(~Q(name__unaccent__icontains="zoe")) == [1, 2, 4]
    assert await matching(name__unaccent=F("name")) == [4]
    assert await TypedRowNote.objects.filter(row__name__unaccent__icontains="gerard").values_list("id", flat=True) == [
        10
    ]
    unaccented = TypedRow.objects.all().order_by("id").values_list("name__unaccent", flat=True)
    assert await unaccented == ["Gerard Depardieu", "Gerhard Schroder", "Zoe Saldana", "word"]
    with pytest.raises(FieldError):
        await matching(number__unaccent=1)


@pytest.mark.asyncio
async def test_trigram_functions(names):
    similarity = dict(
        await TypedRow.objects.annotate(value=TrigramSimilarity("name", "Gerard")).values_list("id", "value")
    )
    distance = dict(
        await TypedRow.objects.annotate(value=TrigramDistance("name", "Gerard")).values_list("id", "value")
    )
    assert similarity[2] > similarity[1] > similarity[4] > similarity[3] == 0
    assert distance == pytest.approx({row_id: 1 - value for row_id, value in similarity.items()})

    word_similarity = dict(
        await TypedRow.objects.annotate(value=TrigramWordSimilarity("Gerhard", "name")).values_list("id", "value")
    )
    word_distance = dict(
        await TypedRow.objects.annotate(value=TrigramWordDistance("Gerhard", "name")).values_list("id", "value")
    )
    assert word_similarity[2] == 1
    assert word_distance == pytest.approx({row_id: 1 - value for row_id, value in word_similarity.items()})

    strict = dict(
        await TypedRow.objects.annotate(value=TrigramStrictWordSimilarity("Depard", "name")).values_list("id", "value")
    )
    strict_distance = dict(
        await TypedRow.objects.annotate(value=TrigramStrictWordDistance("Depard", "name")).values_list("id", "value")
    )
    assert max(strict, key=strict.__getitem__) == 1
    assert strict_distance == pytest.approx({row_id: 1 - value for row_id, value in strict.items()})

    closest = TypedRow.objects.annotate(value=TrigramSimilarity("name", "Zoe")).order_by("-value", "id")
    assert (await closest.values_list("id", flat=True))[0] == 3
    related = TypedRowNote.objects.annotate(value=TrigramSimilarity("row__name", "Gerard")).values_list(
        "value", flat=True
    )
    assert await related == pytest.approx([similarity[1]])


@pytest.mark.asyncio
async def test_a_trigram_similarity_runs_on_its_plan(names):
    def similarity(text):
        return (
            TypedRow.objects.annotate(similarity=TrigramSimilarity("name", text))
            .order_by("-similarity", "id")
            .values_list("id", flat=True)
        )

    first = await similarity("Gerard")
    hits = StatementPlans.hits
    second = await similarity("Zoe")
    assert StatementPlans.hits - hits == 1
    assert first != second
    assert second[0] == 3
