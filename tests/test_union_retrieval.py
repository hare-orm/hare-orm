"""Retrieval methods of a model set operation (``union()``/``intersection()``/``difference()``), like
Django's - ``first()``/``last()``/``get()``/``get_or_none()``/``exists()``/``iterator()``/``stream()``."""

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.exceptions import DoesNotExist, MultipleObjectsReturned, QueryError, UnSupportedError
from hare.query.expressions import Q
from hare.transactions.transactions import Transactions
from tests.testmodels import Event, Reporter, Tournament


@pytest_asyncio.fixture
async def tournaments(db) -> dict[str, Tournament]:
    return {
        name: await Tournament.objects.create(id=number, name=name) for number, name in ((1, "b"), (2, "a"), (3, "c"))
    }


def first_two():
    return Tournament.objects.filter(id__in=[1, 2]).union(Tournament.objects.filter(id__in=[2, 3]))


@pytest.mark.asyncio
async def test_first_and_last(tournaments):
    assert (await first_two().first()).id == 1
    assert (await first_two().last()).id == 3
    assert (await first_two().order_by("name").first()).name == "a"
    assert (await first_two().order_by("name").last()).name == "c"
    assert (await first_two().order_by("-name")[1:].first()).name == "b"
    assert await Tournament.objects.filter(id=9).union(Tournament.objects.filter(id=8)).first() is None
    with pytest.raises(QueryError, match="sliced"):
        first_two()[1:].last()


@pytest.mark.asyncio
async def test_get(tournaments):
    assert (await Tournament.objects.filter(id=1).union(Tournament.objects.filter(id=1)).get()).id == 1
    assert (await first_two().get(name="c")).id == 3
    assert (await first_two().get(Q(name="a") | Q(name="x"))).id == 2
    assert (await first_two().order_by("name")[:1].get()).name == "a"
    with pytest.raises(MultipleObjectsReturned):
        await first_two().get()
    with pytest.raises(DoesNotExist):
        await first_two().get(name="x")
    with pytest.raises(KeyError):
        await first_two().get(name="x", does_not_exist_exception=KeyError)
    with pytest.raises(QueryError, match="sliced"):
        first_two()[:2].get(name="a")


@pytest.mark.asyncio
async def test_get_or_none(tournaments):
    assert (await first_two().get(does_not_exist_exception=None, id=2)).name == "a"
    assert await first_two().get(does_not_exist_exception=None, name="x") is None
    with pytest.raises(MultipleObjectsReturned):
        await first_two().get(does_not_exist_exception=None, id__gte=2)


@pytest.mark.asyncio
async def test_conditions_filter_every_branch_of_each_set_operation(tournaments):
    everything = Tournament.objects.all()
    assert (await everything.difference(Tournament.objects.filter(id=1)).get(name__in=["a", "b"])).id == 2
    assert (
        await everything.difference(Tournament.objects.filter(id=1)).get(does_not_exist_exception=None, name="b")
        is None
    )
    assert (await everything.intersection(Tournament.objects.filter(id__in=[1, 3])).get(id__lte=1)).id == 1
    nested = Tournament.objects.filter(id=1).union(
        Tournament.objects.filter(id=2).union(Tournament.objects.filter(id=3))
    )
    assert (await nested.get(name="c")).id == 3


@pytest.mark.asyncio
async def test_exists(tournaments):
    assert await first_two().exists() is True
    assert await first_two()[3:].exists() is False
    assert await Tournament.objects.filter(id=9).union(Tournament.objects.filter(id=8)).exists() is False
    assert await Tournament.objects.all().difference(Tournament.objects.all()).exists() is False


@pytest.mark.asyncio
async def test_iterator(tournaments):
    union = first_two().order_by("-name")
    assert [tournament.name async for tournament in union.iterator(chunk_size=1)] == ["c", "b", "a"]
    assert [tournament.name async for tournament in union[1:].iterator(chunk_size=1)] == ["b", "a"]
    assert [tournament.name async for tournament in union[:2].iterator(chunk_size=5)] == ["c", "b"]
    union_all = Tournament.objects.filter(id=1).union(Tournament.objects.all(), all=True).order_by("name")
    assert [tournament.id async for tournament in union_all.iterator(chunk_size=1)] == [2, 1, 1, 3]
    assert [tournament.id async for tournament in first_two().iterator(chunk_size=1)] == [1, 2, 3]
    names = first_two().values_list("name", flat=True)
    assert [name async for name in names.iterator(chunk_size=1)] == ["a", "b", "c"]
    with pytest.raises(QueryError, match="chunk_size"):
        async for _ in union.iterator(chunk_size=0):
            pass


@pytest.mark.asyncio
async def test_iterator_over_several_models(tournaments):
    await Reporter.objects.create(id=1, name="b")
    union = (
        Tournament.objects.all().only("id", "name").union(Reporter.objects.all().only("id", "name")).order_by("name")
    )
    rows = [(type(row).__name__, row.id) async for row in union.iterator(chunk_size=1)]
    assert rows == [("Tournament", 2), ("Reporter", 1), ("Tournament", 1), ("Tournament", 3)]


@pytest.mark.asyncio
async def test_prefetch_related_with_first(tournaments):
    await Event.objects.create(name="e", tournament=tournaments["b"])
    tournament = await first_two().prefetch_related("events").first()
    assert [event.name for event in tournament.events] == ["e"]


@pytest.mark.asyncio
@requires_features(supports_streaming=False)
async def test_stream_raises_unsupported_without_streaming(tournaments):
    with pytest.raises(UnSupportedError, match="stream"):
        async for _ in first_two().stream():
            pass


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream(tournaments):
    async with Transactions.atomic():
        streamed = [tournament.id async for tournament in first_two().order_by("id").stream(chunk_size=1)]
        mixed = [
            (type(row).__name__, row.name)
            async for row in Tournament.objects.filter(id=1)
            .only("id", "name")
            .union(Reporter.objects.all().only("id", "name"))
            .order_by("name")
            .stream()
        ]
    assert streamed == [1, 2, 3]
    assert mixed == [("Tournament", "b")]
    with pytest.raises(QueryError, match="prefetch_related"):
        async with Transactions.atomic():
            async for _ in first_two().prefetch_related("events").stream():
                pass


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_requires_active_transaction(db_simple):
    with pytest.raises(QueryError, match="Transactions.atomic"):
        async for _ in first_two().stream():
            pass
