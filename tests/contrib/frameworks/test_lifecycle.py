"""HareLifecycle: the Hare context of an application's lifetime."""

import pytest

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.contrib.frameworks.hare_lifecycle import HareLifecycle
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from tests.contrib.frameworks.models import Writer

REQUIRES = ("pydantic",)

pytestmark = pytest.mark.sqlite_only

MODEL_MODULES = ["tests.contrib.frameworks.models"]
APPLICATION_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": MODEL_MODULES}},
}


@pytest.mark.asyncio
async def test_stopping_leaves_the_context_of_the_calling_task_open():
    async with hare_test_context(MODEL_MODULES) as outer_context:
        await Writer.objects.create(name="anna")
        lifecycle = HareLifecycle(APPLICATION_CONFIG)
        await lifecycle.start()
        application_context = lifecycle.context
        await lifecycle.stop()

        assert HareContext.get_current() is outer_context
        assert application_context is not None and application_context._connections is None
        assert await Writer.objects.all().count() == 1


@pytest.mark.asyncio
async def test_a_test_context_stays_the_global_one_until_its_last_application_stops():
    async with hare_test_context(MODEL_MODULES) as test_context:
        ApplicationLifecycle.test_context = test_context
        try:
            web_application, task_worker = HareLifecycle(APPLICATION_CONFIG), HareLifecycle(APPLICATION_CONFIG)
            await web_application.start()
            await task_worker.start()

            await web_application.stop()
            assert HareContext.global_context is test_context
            await task_worker.stop()
            assert HareContext.global_context is None
        finally:
            ApplicationLifecycle.test_context = None


@pytest.mark.parametrize(
    "transactions", [5, "default", None, [1], [""]], ids=["number", "text", "none", "number in list", "empty name"]
)
def test_transactions_neither_a_bool_nor_connection_names_are_refused(transactions):
    """A number raised a raw TypeError, and a text was read as the names of its letters."""
    with pytest.raises(ConfigurationError, match="atomic_requests is True, False or a sequence of connection names"):
        HareLifecycle(APPLICATION_CONFIG, atomic_requests=transactions)


def test_transactions_on_named_connections_are_taken():
    assert HareLifecycle(APPLICATION_CONFIG, atomic_requests=("default", "reports")).transaction_connection_names == (
        "default",
        "reports",
    )
