import inspect

import pytest

from hare.fields.field import Field
from tests.shared_models import testmodels


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


@pytest.mark.database_independent
def test_callable_object_async_call_detected_as_async_default():
    """Field._is_async_default() must check `obj.__call__` to detect this shape."""
    assert Field._is_async_default(testmodels.AsyncCallableObjectDefault()) is True
    # inspect.iscoroutinefunction() alone is False for this exact shape - documents why
    # the naive check used to miss it.
    assert inspect.iscoroutinefunction(testmodels.AsyncCallableObjectDefault()) is False


@pytest.mark.database_independent
def test_partial_wrapped_async_function_detected_as_async_default():
    """functools.partial wrapping an async function is still detected as an async default.

    inspect.iscoroutinefunction() already unwraps functools.partial (recursively) on
    this project's target Python (>=3.14) - kept as a regression test for
    Field._is_async_default(), which delegates straight to it for this shape.
    """
    assert Field._is_async_default(testmodels.async_partial_default) is True
    assert inspect.iscoroutinefunction(testmodels.async_partial_default) is True


async def save_a_new_callable_default():
    model = testmodels.CallableDefault()
    await model.save()
    return model


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        pytest.param("async_callable_object_default", "async_callable_object_default", id="callable_object"),
        pytest.param("async_partial_default", "async_partial_default_value", id="partial_wrapped"),
    ],
)
@pytest.mark.parametrize(
    "make_saved",
    [
        pytest.param(lambda: testmodels.CallableDefault.objects.create(), id="create"),
        pytest.param(save_a_new_callable_default, id="by_save"),
    ],
)
@pytest.mark.asyncio
async def test_an_async_default_is_filled(db, make_saved, field_name, expected):
    model = await make_saved()
    assert getattr(model, field_name) == expected
