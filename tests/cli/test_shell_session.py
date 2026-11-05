import asyncio
from contextlib import asynccontextmanager

import pytest

from hare.cli.shell_session import ShellSession
from hare.core.hare_context import HareContext

pytestmark = pytest.mark.database_independent


def test_shell_session_preserves_loop_context_and_asyncio_functions():
    current_task = asyncio.current_task
    task_implementation = asyncio.Task
    lifecycle = []

    @asynccontextmanager
    async def application():
        async with HareContext() as context:
            lifecycle.append(asyncio.current_task())
            try:
                yield context
            finally:
                lifecycle.append(asyncio.current_task())

    async def cell():
        assert HareContext.get_current() is not None
        async with asyncio.timeout(5):
            async with asyncio.TaskGroup() as group:
                task = group.create_task(asyncio.sleep(0, asyncio.get_running_loop()))
            return await asyncio.wait_for(task, 5)

    async def failed_cell():
        raise ValueError("cell failure")

    async def cancelled_cell():
        raise asyncio.CancelledError

    with ShellSession(application()) as session:
        loop = session.run_cell(cell())
        with pytest.raises(ValueError, match="cell failure"):
            session.run_cell(failed_cell())
        with pytest.raises(asyncio.CancelledError):
            session.run_cell(cancelled_cell())
        assert session.run_cell(cell()) is loop
    assert loop.is_closed()
    assert lifecycle[0] is lifecycle[1]
    assert HareContext.get_current() is None
    assert asyncio.current_task is current_task
    assert asyncio.Task is task_implementation


def test_shell_closes_background_cells_before_the_application():
    events = []

    @asynccontextmanager
    async def application():
        async with HareContext() as context:
            yield context
            events.append("application closed")

    async def start_background_task():
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def background():
            try:
                started.set()
                await blocked.wait()
            finally:
                events.append("cell closed")

        task = asyncio.create_task(background())
        await started.wait()
        return task

    with ShellSession(application()) as session:
        task = session.run_cell(start_background_task())
    assert task.cancelled()
    assert events == ["cell closed", "application closed"]


def test_shell_startup_failure_restores_the_application_context():
    @asynccontextmanager
    async def application():
        async with HareContext() as context:
            raise ValueError("startup failure")
            yield context

    session = ShellSession(application())
    with pytest.raises(ValueError, match="startup failure"), session:
        raise AssertionError("startup should fail")
    assert HareContext.get_current() is None
