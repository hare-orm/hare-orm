from __future__ import annotations

import asyncio

from hare.dialects.postgresql.schema.constants import POSTGRESQL_MIGRATION_LOCK_KEY


class MigrationLockWaiter:
    """Observe the actual migrator's backend and its waiting advisory lock.

    The hook records the backend PID on the transaction executing the lock command.
    The holder independently queries the server catalog, restricted to that PID,
    database and bigint lock key. Other migrations cannot satisfy the observation.
    """

    def __init__(self, holder, lock_sql: str, monkeypatch) -> None:
        self.holder = holder
        self.backend: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        client_type = type(holder)
        execute_script = client_type.execute_script

        async def observe(client, script, *args, **kwargs):
            if script == lock_sql and client is not holder and not self.backend.done():
                (row,) = await client.execute_dicts("SELECT pg_backend_pid() AS pid")
                self.backend.set_result(row["pid"])
            return await execute_script(client, script, *args, **kwargs)

        monkeypatch.setattr(client_type, "execute_script", observe)

    async def wait(self, migrate: asyncio.Task) -> None:
        async with asyncio.timeout(30):
            await asyncio.wait((migrate, self.backend), return_when=asyncio.FIRST_COMPLETED)
            if migrate.done():
                await migrate
                raise AssertionError("migrate finished before taking its migration lock")
            pid = self.backend.result()
            key = POSTGRESQL_MIGRATION_LOCK_KEY
            while True:
                rows = await self.holder.execute_dicts(
                    "SELECT count(*) AS waiting FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted "
                    f"AND pid = {pid} AND classid = {key >> 32} AND objid = {key & 0xFFFFFFFF} "
                    "AND objsubid = 1 "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                )
                if rows[0]["waiting"] == 1:
                    return
                if migrate.done():
                    await migrate
                    raise AssertionError("migrate finished without waiting for its migration lock")
                # Yield even when a driver completes a catalog query without suspending.
                await asyncio.sleep(0)
