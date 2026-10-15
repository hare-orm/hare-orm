from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.transactions.enums import IsolationLevel

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Sequence

    from hare.dialects.base.dialect import Dialect


class TransactionStatements:
    """The statements that set a transaction up right after ``BEGIN``: its isolation level, read
    only, its statement and lock timeouts. This base writes ISO SQL; a restriction it has no
    statement for is None.
    """

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose transactions are set up.
        """
        self.dialect = dialect

    def get_begin_sql(self) -> str:
        """The statement opening a transaction - written around the SQL of an atomic migration."""
        return "START TRANSACTION"

    def get_commit_sql(self) -> str:
        """The statement committing a transaction."""
        return "COMMIT"

    def get_rollback_sql(self) -> str:
        """The statement rolling a transaction back."""
        return "ROLLBACK"

    def get_isolation_level(self, requested: IsolationLevel) -> IsolationLevel:
        """Returns the isolation level a transaction asking for ``requested`` runs at: the
        weakest of ``Features.isolation_levels`` at least as strong as ``requested``. The SQL
        standard lets a database run a transaction at a stronger level than the one asked for,
        never a weaker one.

        Args:
            requested: The level asked for.

        Returns:
            The level the transaction runs at.

        Raises:
            UnSupportedError: The database has no level at least as strong as ``requested``.
        """
        strength_order = list(IsolationLevel)
        for level in self.dialect.features.isolation_levels:
            if strength_order.index(level) >= strength_order.index(requested):
                return level
        raise UnSupportedError(
            f"The {self.dialect} dialect runs no transaction at the {requested!s} isolation level or a stronger one"
        )

    def get_isolation_level_sql(self, level: IsolationLevel) -> str | None:
        """Returns the statement that makes a just-begun transaction run at ``level``, one of
        ``Features.isolation_levels``.

        Args:
            level: The isolation level.

        Returns:
            The statement, None when every transaction already runs at that level.
        """
        return f"SET TRANSACTION ISOLATION LEVEL {level.upper()}"

    def get_read_only_sql(self) -> str | None:
        """The statement making a just-begun transaction read-only.

        Returns:
            The statement, None when the database has none.
        """
        return "SET TRANSACTION READ ONLY"

    def get_statement_timeout_sql(self, milliseconds: int) -> str | None:
        """The statement cancelling any statement of the transaction that runs longer.

        Args:
            milliseconds: The timeout, at least 1.

        Returns:
            The statement, None when the database has none.
        """
        return None

    def get_lock_timeout_sql(self, milliseconds: int) -> str | None:
        """The statement cancelling any statement of the transaction that waits longer for a lock.

        Args:
            milliseconds: The timeout, at least 1.

        Returns:
            The statement, None when the database has none.
        """
        return None

    def get_tenant_setting_sql(self, tenant_texts: Sequence[str] | None) -> str:
        """The statement setting the tenants a ``TenantCondition`` policy lets the transaction see.

        Args:
            tenant_texts: The tenant values as text; None for every tenant.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The database has no row level security - by default.
        """
        raise UnSupportedError(f"{self.dialect.name} has no row level security tenancy")

    def get_session_lock_timeout_sql(self, milliseconds: int) -> str | None:
        """The statement cancelling any later statement of the connection, in a transaction or not,
        that waits longer for a lock - for statements that can't run in a transaction.

        Args:
            milliseconds: The timeout, at least 1.

        Returns:
            The statement, None when the database has none.
        """
        return None
