"""HareLifecycle: the Hare context of an application's lifetime."""

import pytest

from hare.contrib.frameworks.hare_lifecycle import HareLifecycle
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from tests.contrib.frameworks.models import Writer

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
async def test_applications_borrow_only_the_explicit_context_and_leave_it_open():
    async with hare_test_context(MODEL_MODULES) as test_context:
        previous_fallback = HareContext.global_context
        web_application = HareLifecycle(APPLICATION_CONFIG, context=test_context)
        task_worker = HareLifecycle(APPLICATION_CONFIG, context=test_context)
        await web_application.start()
        await task_worker.start()
        with web_application.scope():
            assert HareContext.require_current() is test_context
        await web_application.stop()
        assert HareContext.global_context is previous_fallback
        assert task_worker.context is test_context and test_context._inited
        await task_worker.stop()
        assert test_context._inited
        assert HareContext.global_context is previous_fallback


@pytest.mark.parametrize(
    "transactions",
    [5, "default", b"", None, [1], [""]],
    ids=["number", "text", "bytes", "none", "number in list", "empty name"],
)
def test_transactions_neither_a_bool_nor_connection_names_are_refused(transactions):
    """A number raised a raw TypeError, and a text was read as the names of its letters."""
    with pytest.raises(ConfigurationError, match="atomic_requests is True, False or an iterable of connection names"):
        HareLifecycle(APPLICATION_CONFIG, atomic_requests=transactions)


@pytest.mark.parametrize(
    "container",
    [tuple, list, iter, lambda values: (name for name in values)],
    ids=["tuple", "list", "iterator", "generator"],
)
def test_transactions_on_named_connections_are_taken(container):
    names = ["default", "reports"]
    transactions = container(names)
    lifecycle = HareLifecycle(APPLICATION_CONFIG, atomic_requests=transactions)
    names.append("later")
    if isinstance(transactions, list):
        transactions.append("changed")
    assert lifecycle.transaction_connection_names == ("default", "reports")
