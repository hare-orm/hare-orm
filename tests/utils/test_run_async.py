"""Tests for run_async function.

These tests verify that run_async properly cleans up Hare state after execution.
Since run_async is designed to run a coroutine and clean up all Hare state afterwards,
we need to verify this cleanup behavior works correctly.

Note: These tests require no active HareContext when they start. If run after tests
that use the session-scoped `db` fixture, they will be skipped.
"""

import os

import pytest

from hare import Hare
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext


@pytest.fixture
def holder():
    return {"value": 1}


@pytest.fixture
def requires_no_context():
    """Skip the test if there's already an active HareContext."""
    if HareContext.get_current() is not None:
        pytest.skip("Test requires no active HareContext - run in isolation")


async def init_and_check(holder):
    """Initialize Hare and verify context is set up."""
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))
    holder["value"] = 2
    # Verify we have an active context
    ctx = HareContext.get_current()
    assert ctx is not None
    assert ctx.connections._get_storage() != {}


async def init_and_raise(holder):
    """Initialize Hare and raise an exception."""
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))
    holder["value"] = 3
    # Verify we have an active context
    ctx = HareContext.get_current()
    assert ctx is not None
    assert ctx.connections._get_storage() != {}
    raise Exception("Some exception")


@pytest.mark.skipif(os.name == "nt", reason="stuck with Windows")
def test_run_async(holder, requires_no_context):
    """Test that run_async properly cleans up after successful execution."""
    # No context should be active before run_async
    assert HareContext.get_current() is None
    assert holder["value"] == 1

    Hare.run_async(init_and_check(holder))

    # After run_async, context should be cleaned up
    assert HareContext.get_current() is None
    assert holder["value"] == 2


@pytest.mark.skipif(os.name == "nt", reason="stuck with Windows")
def test_run_async_raised(holder, requires_no_context):
    """Test that run_async properly cleans up even when an exception is raised."""
    # No context should be active before run_async
    assert HareContext.get_current() is None
    assert holder["value"] == 1

    with pytest.raises(Exception, match="Some exception"):
        Hare.run_async(init_and_raise(holder))

    # After run_async (even with exception), context should be cleaned up
    assert HareContext.get_current() is None
    assert holder["value"] == 3
