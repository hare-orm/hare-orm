from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from hare.dialects.base.constants import DEFAULT_MAX_BIND_PARAMETERS


@dataclass(frozen=True, slots=True)
class Features:
    """What a connection's database and driver support, fixed once the connection is created.

    Attributes:
        supports_transactions: Transactions and savepoints.
        can_rollback_ddl: DDL runs inside a transaction and rolls back with it.
        supports_select_for_update: ``SELECT ... FOR UPDATE``.
        supports_select_for_no_key_update: ``SELECT ... FOR NO KEY UPDATE``.
        supports_update_limit_order_by: ``UPDATE``/``DELETE`` with ``ORDER BY`` and ``LIMIT``.
        supports_posix_regex: POSIX regular expression lookups.
        supports_returning: ``INSERT ... RETURNING``.
        supports_two_phase_commit: ``PREPARE TRANSACTION``/``COMMIT PREPARED``.
        supports_listen_notify: ``LISTEN``/``NOTIFY``.
        inline_comments: Table and column comments go inside ``CREATE TABLE``.
        supports_positional_rows: Result rows read both by position and by column name.
        supports_streaming: Rows are paged off a server-side cursor.
        execute_many_scales_poorly: ``executemany()`` is slower than one multi-row statement.
        max_bind_parameters: Most bind parameters one statement may carry.
        cascade_depth_limit: The recursion depth a native ``ON DELETE CASCADE`` stops at, None for
            none. The client then raises ``CascadeDepthLimitError`` (or a subclass) when it stops,
            and ``delete()`` of a model with a cascade cycle retries such a DELETE with a
            Python-side cascade walk, deleting fewer levels than this at a time.
        supports_nulls_distinct: A unique constraint takes ``NULLS [NOT] DISTINCT``.
        supports_partitioned_exclusion_constraints: A partitioned table takes an exclusion constraint.
    """

    supports_transactions: bool = True
    can_rollback_ddl: bool = False
    supports_select_for_update: bool = True
    supports_select_for_no_key_update: bool = False
    supports_update_limit_order_by: bool = True
    supports_posix_regex: bool = False
    supports_returning: bool = False
    supports_two_phase_commit: bool = False
    supports_listen_notify: bool = False
    inline_comments: bool = False
    supports_positional_rows: bool = False
    supports_streaming: bool = False
    execute_many_scales_poorly: bool = False
    max_bind_parameters: int = DEFAULT_MAX_BIND_PARAMETERS
    cascade_depth_limit: int | None = None
    supports_nulls_distinct: bool = False
    supports_partitioned_exclusion_constraints: bool = False

    def replace(self, **overrides: Any) -> Features:
        """A copy with the given features changed.

        Args:
            overrides: Feature values by name.

        Returns:
            The new features.
        """
        return dataclasses.replace(self, **overrides)
