from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.asyncpg_extension_type_codecs import AsyncpgExtensionTypeCodecs

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient


class AsyncpgSchemaCodecs:
    """Refreshes extension codecs on checked-out asyncpg connections after schema changes."""

    @staticmethod
    def forget_schema_connections(client: AsyncpgClient, pool: asyncpg.Pool) -> None:
        """Drop a retired pool's snapshots without retaining pools in cache keys.

        asyncpg pools cannot be weakly referenced. Initialization replaces each physical
        connection's entry before checkout; close and abandon remove its pool identity.
        """
        pool_id = id(pool)
        snapshots = client.schema_connections.get_bucket(client)
        for key in tuple(snapshots):
            if key[0] == pool_id:
                snapshots.pop(key, None)
        client.schema_retirements.get_bucket(client).pop((pool_id,), None)

    @staticmethod
    async def refresh_schema_codecs(client: AsyncpgClient, connection: Any, pool: asyncpg.Pool) -> None:
        """Refresh one checked-out connection before use when its schema generation changed.

        The caller holds the physical connection's statement lock. Cache eviction is safe:
        a missing snapshot refreshes metadata and retires connections with unknown old codecs.
        Only removal of an installed OID requires retiring a known connection; ordinary DDL
        and newly installed extensions do not reconnect the pool.
        """
        key = (id(pool), connection.get_server_pid())
        snapshots = client.schema_connections.get_bucket(client)
        while True:
            generation = client.schema_generation
            previous = snapshots.get(key)
            if previous is not None and previous[0] == generation:
                return
            # A failed installation may already have registered new OIDs. Its partial
            # registry must remain unknown, so the next refresh retires it conservatively.
            snapshots.pop(key, None)
            await connection.reload_schema_state()
            installed_oids = await AsyncpgExtensionTypeCodecs.install(connection)
            if previous is None or previous[1] - installed_oids:
                retirements = client.schema_retirements.get_bucket(client)
                if retirements.get((id(pool),)) != generation:
                    await pool.expire_connections()
                    retirements.store((id(pool),), generation)
            snapshots.store(key, (generation, installed_oids))
