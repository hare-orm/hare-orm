"""HareLifecycle: the Hare context of an application's lifetime."""

import pytest

from hare.contrib.frameworks import HareLifecycle
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from tests.contrib.frameworks.models import Writer

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
