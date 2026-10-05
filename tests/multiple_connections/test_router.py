import pytest

from hare.core.routing.connection_router import ConnectionRouter
from hare.exceptions import ConfigurationError
from tests.shared_models.testmodels import Tournament


class _NoWriteRouter:
    """Missing db_for_write entirely - _router_func must skip it via AttributeError, not crash."""

    def db_for_read(self, model):
        return "models"


class _FalsyRouter:
    """Methods return a falsy value - _router_func must fall through to the next router."""

    def db_for_read(self, model):
        return None

    def db_for_write(self, model):
        return ""


class _RealRouter:
    def db_for_read(self, model):
        return "models"

    def db_for_write(self, model):
        return "models"


class _UnknownConnectionRouter:
    def db_for_read(self, model):
        return "does-not-exist"


class _NotCallableRouter:
    db_for_read = "models"


@pytest.mark.asyncio
async def test_no_routers_returns_none(db):
    router = ConnectionRouter()
    assert router.db_for_read(Tournament) is None
    assert router.db_for_write(Tournament) is None


@pytest.mark.parametrize(
    ("first_router", "method_name"),
    [
        pytest.param(_NoWriteRouter, "db_for_write", id="router_missing_method_is_skipped"),
        pytest.param(_FalsyRouter, "db_for_read", id="falsy_result_falls_through_to_next_router"),
    ],
)
@pytest.mark.asyncio
async def test_a_router_without_an_answer_falls_through_to_the_next(db, first_router, method_name):
    router = ConnectionRouter()
    router.init_routers([first_router, _RealRouter])
    connection = getattr(router, method_name)(Tournament)
    assert connection is not None
    assert connection.connection_alias == "models"


@pytest.mark.asyncio
async def test_unknown_connection_name_raises(db):
    """A router that resolves to a nonexistent alias is a real misconfiguration (typo, stale
    alias after a connection was renamed/removed) - distinct from a router returning None/not
    applying at all, which is the legitimate "no router matched" case. Silently falling back to
    the default connection here would mask a broken router indefinitely."""
    router = ConnectionRouter()
    router.init_routers([_UnknownConnectionRouter])
    with pytest.raises(ConfigurationError):
        router.db_for_read(Tournament)
