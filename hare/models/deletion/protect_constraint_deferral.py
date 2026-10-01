from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, ClassVar

from hare.core.cache import Cache
from hare.models.deletion.deletion_graph import DeletionGraph

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class ProtectConstraintDeferral:
    """Deferring the database's PROTECT backstops for the time a hard delete runs, where the database
    checks them after every nested cascade step - a protector the same cascade removes too must
    not fail the delete."""

    #: (model, connection name) -> the names of the deferrable constraints behind the PROTECT
    #: foreign keys a hard delete of the model's rows can run into.
    CONSTRAINT_NAMES_CACHE: ClassVar[Cache[tuple[str, ...]]] = Cache(
        Cache.max_size_from_env(), holds_sql=False, depends_on_other_models=True
    )

    #: Connection names whose current transaction already runs a deferred delete - a delete nested
    #: inside it (a per-row fallback, a Python-side cascade step) leaves the deferral to the outer one.
    deferring_connection_names: ContextVar[frozenset[str]] = ContextVar(
        "protect_deferral_connection_names", default=frozenset()
    )

    @staticmethod
    def is_needed(model: type[Model], db: DatabaseClient) -> bool:
        """Whether a ``DELETE`` of ``model`` rows on ``db`` needs its PROTECT backstops deferred -
        where the database checks a ``NO ACTION`` constraint after every cascade step, a protector
        removed later still fails it.

        Args:
            model: The model rows are deleted from.
            db: The connection the delete runs on.

        Returns:
            Whether the delete has to run inside ``defer()``.
        """
        return db.dialect.checks_foreign_keys_per_cascade_step and DeletionGraph.has_transitive_protect(model)

    @staticmethod
    def forget_constraint_names() -> None:
        """Drops every cached PROTECT constraint name - DDL may have renamed or rebuilt them."""
        ProtectConstraintDeferral.CONSTRAINT_NAMES_CACHE.clear()

    @staticmethod
    @asynccontextmanager
    async def defer(model: type[Model], db: DatabaseClient) -> AsyncGenerator[None]:
        """Defers, for the block only, the PROTECT backstops a hard delete of ``model`` rows can run
        into; they are checked again when the block ends. A block nested in another deferral leaves
        it to the outer one.

        Args:
            model: The model rows are deleted from.
            db: A client inside the transaction the delete runs in.

        Raises:
            IntegrityError: A deferred constraint still fails once the block ends.
            UnSupportedError: The dialect has no way to defer them.
        """
        active_connection_names = ProtectConstraintDeferral.deferring_connection_names.get()
        if db.connection_name in active_connection_names or not DeletionGraph.has_transitive_protect(model):
            yield
            return
        async with db.dialect.defer_cascade_foreign_keys(model, db) as deferred:
            if not deferred:
                yield
                return
            token = ProtectConstraintDeferral.deferring_connection_names.set(
                active_connection_names | {db.connection_name}
            )
            try:
                yield
            finally:
                ProtectConstraintDeferral.deferring_connection_names.reset(token)
