"""A query asks the router for its connection once per execution - the connection a plan lookup chose is the
one the execution builds on, on a cache miss as on a hit."""

import pytest

from hare.core.caching.caches import Caches
from hare.core.hare_context import HareContext
from tests.shared_models.testmodels import Tournament


class CountingRouter:
    """Counts the router's choices of a read connection."""

    read_calls = 0

    def db_for_read(self, model):
        CountingRouter.read_calls += 1

    def db_for_write(self, model):
        return None


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param(lambda queryset: queryset.count(), id="count"),
        pytest.param(lambda queryset: queryset.exists(), id="exists"),
        pytest.param(lambda queryset: queryset, id="rows"),
        pytest.param(lambda queryset: queryset.values_list("id", flat=True), id="values"),
    ],
)
@pytest.mark.asyncio
async def test_each_execution_asks_the_router_once(db, summary):
    await Tournament.objects.create(name="routed")
    Caches.forget_model_caches([Tournament])
    router = HareContext.require_current().router
    router.init_routers([CountingRouter])
    try:
        for _ in range(3):
            CountingRouter.read_calls = 0
            await summary(Tournament.objects.filter(name="routed"))
            assert CountingRouter.read_calls == 1
    finally:
        router.init_routers([])
