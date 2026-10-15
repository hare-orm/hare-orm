import inspect

import pytest

from hare.fields.field import Field
from tests import testmodels


@pytest.mark.asyncio
async def test_default_create(db):
    model = await testmodels.CallableDefault.objects.create()
    assert model.callable_default == "callable_default"
    assert model.async_default == "async_callable_default"


@pytest.mark.asyncio
async def test_default_by_save(db):
    saved_model = testmodels.CallableDefault()
    await saved_model.save()
    assert saved_model.callable_default == "callable_default"
    assert saved_model.async_default == "async_callable_default"


@pytest.mark.asyncio
async def test_async_default_change(db):
    default_change = testmodels.CallableDefault()
    default_change.async_default = "changed"
    await default_change.save()
    assert default_change.async_default == "changed"


# ============================================================================
# Async default as a callable OBJECT (not a bare `async def` function) - calling it still
# returns a genuine coroutine, but inspect.iscoroutinefunction() alone only recognizes a
# bare `async def` function/method (including one wrapped in functools.partial), so this
# shape used to leave the un-awaited coroutine itself saved as the field's value.
# ============================================================================


def test_callable_object_async_call_detected_as_async_default():
    """Field._is_async_default() must check `obj.__call__` to detect this shape."""
    assert Field._is_async_default(testmodels.AsyncCallableObjectDefault()) is True
    # inspect.iscoroutinefunction() alone is False for this exact shape - documents why
    # the naive check used to miss it.
    assert inspect.iscoroutinefunction(testmodels.AsyncCallableObjectDefault()) is False


def test_partial_wrapped_async_function_detected_as_async_default():
    """functools.partial wrapping an async function is still detected as an async default.

    inspect.iscoroutinefunction() already unwraps functools.partial (recursively) on
    this project's target Python (>=3.14) - kept as a regression test for
    Field._is_async_default(), which delegates straight to it for this shape.
    """
    assert Field._is_async_default(testmodels.async_partial_default) is True
    assert inspect.iscoroutinefunction(testmodels.async_partial_default) is True


@pytest.mark.asyncio
async def test_callable_object_async_default_create(db):
    model = await testmodels.CallableDefault.objects.create()
    assert model.async_callable_object_default == "async_callable_object_default"


@pytest.mark.asyncio
async def test_callable_object_async_default_by_save(db):
    saved_model = testmodels.CallableDefault()
    await saved_model.save()
    assert saved_model.async_callable_object_default == "async_callable_object_default"


@pytest.mark.asyncio
async def test_partial_wrapped_async_default_create(db):
    model = await testmodels.CallableDefault.objects.create()
    assert model.async_partial_default == "async_partial_default_value"


@pytest.mark.asyncio
async def test_partial_wrapped_async_default_by_save(db):
    saved_model = testmodels.CallableDefault()
    await saved_model.save()
    assert saved_model.async_partial_default == "async_partial_default_value"
