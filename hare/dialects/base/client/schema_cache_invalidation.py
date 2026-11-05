from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.constants import SCHEMA_CACHE_INVALIDATION_FAILED

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class SchemaCacheInvalidation:
    """Publish schema visibility changes after DDL or completion of a prepared transaction."""

    @staticmethod
    @asynccontextmanager
    async def run(client: DatabaseClient, *, schema_change: bool = True) -> AsyncGenerator[None]:
        """Invalidate after success, partial execution or cancellation, retaining both failures.

        The client may be the originating pool when another connection executes the command.
        Already started invalidation completes before the caller receives cancellation.
        """
        if schema_change:
            # Local import: model deletion depends on the client protocol.
            from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral

            ProtectConstraintDeferral.forget_constraint_names()
        try:
            yield
        except BaseException as error:
            if schema_change:
                try:
                    await ResourceCompletion.wait(client.invalidate_schema_cache())
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(SCHEMA_CACHE_INVALIDATION_FAILED, [error, cleanup_error]) from None
            raise
        else:
            if schema_change:
                await ResourceCompletion.wait(client.invalidate_schema_cache())
        finally:
            if schema_change:
                ProtectConstraintDeferral.forget_constraint_names()
