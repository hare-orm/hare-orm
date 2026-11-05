from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, ClassVar, TypeVar, cast

import pytest

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.sqlite.client.sqlite_client import SqliteClient

TResult = TypeVar("TResult")


class NativePoolAcquisitionGate:
    """Pause before Rust acquires internally; every operation still reaches the real pool."""

    def __init__(self, pool: Any, gate: ConnectionAcquisitionGate) -> None:
        self.pool = pool
        self.gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self.pool, name)

    async def fetch_all(self, *args: Any, **kwargs: Any) -> Any:
        await self.gate.wait()
        return await self.pool.fetch_all(*args, **kwargs)


class ConnectionAcquisitionGate:
    """Pause real ORM operations after choosing a client but before acquiring its connection.

    Worker context propagates into the ORM's resource-completion tasks. The controller
    can close or replace the client while every worker is at that exact boundary;
    no timing delay or simulated SQL result establishes the schedule.
    """

    current_worker: ClassVar[ContextVar[tuple[ConnectionAcquisitionGate, int] | None]] = ContextVar(
        "connection_acquisition_worker", default=None
    )

    def __init__(self, workers: int = 4) -> None:
        self.workers = workers
        self.waiting: set[int] = set()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        worker = self.current_worker.get()
        if worker is None or worker[0] is not self or worker[1] in self.waiting:
            return
        self.waiting.add(worker[1])
        if len(self.waiting) == self.workers:
            self.ready.set()
        await self.release.wait()

    async def run_worker(self, number: int, operation: Callable[[int], Awaitable[TResult]]) -> TResult:
        token = self.current_worker.set((self, number))
        try:
            return await operation(number)
        finally:
            self.current_worker.reset(token)

    async def run(
        self,
        client: DatabaseClient,
        operation: Callable[[int], Awaitable[TResult]],
        replace_connection: Callable[[], Awaitable[Any]],
    ) -> list[TResult]:
        if client.dialect.name == "postgresql":
            target, method = client, "_pool_acquire"
        else:
            assert client.dialect.name == "sqlite"
            target, method = cast("SqliteClient", client)._lock, "acquire"
        target_type = type(target)
        original = getattr(target_type, method)
        native_gate = None
        if type(client).__module__.startswith("hare.dialects.postgresql.drivers.rust_pg."):
            native_gate = NativePoolAcquisitionGate(client._pool, self)
            client._pool = native_gate

        async def acquire(instance, *args, **kwargs):
            if instance is target:
                await self.wait()
            return await original(instance, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(target_type, method, acquire)
            tasks = [asyncio.create_task(self.run_worker(number, operation)) for number in range(self.workers)]
            ready = asyncio.create_task(self.ready.wait())
            try:
                async with asyncio.timeout(30):
                    await asyncio.wait((*tasks, ready), return_when=asyncio.FIRST_COMPLETED)
                    for task in tasks:
                        if task.done():
                            await task
                            raise AssertionError("An ORM operation finished before reaching connection acquisition")
                    assert self.ready.is_set()
                    await replace_connection()
                    self.release.set()
                    return list(await asyncio.gather(*tasks))
            finally:
                self.release.set()
                for task in (*tasks, ready):
                    if not task.done():
                        task.cancel()
                await ResourceCompletion.wait(asyncio.gather(*tasks, ready, return_exceptions=True))
                if native_gate is not None and client._pool is native_gate:
                    client._pool = native_gate.pool
