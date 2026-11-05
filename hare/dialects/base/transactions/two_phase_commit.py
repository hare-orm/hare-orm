from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.exceptions import ConfigurationError, OperationalError


class TwoPhaseCommit(abc.ABC):
    """The statements of the two-phase commit ``Transactions.distributed()`` runs on a dialect.

    Every participant's transaction is prepared under a global transaction id (GID): its writes
    survive a crash and become visible once the GID is committed, from any connection. The
    coordinator records its decision to commit in ``hare_distributed_decisions`` inside its own
    transaction, so recovery can tell a prepared participant to commit from one to roll back. The
    prepare/commit/rollback statements take the GID as a quoted literal; the others bind their
    values.

    Attributes:
        like_escape_character: The escape character of the ``LIKE`` pattern
            ``get_prepared_listing_sql`` matches GIDs with.
    """

    like_escape_character: str = "\\"

    @abc.abstractmethod
    def get_prepare_sql(self, gid_literal: str) -> str:
        """Returns the statement ending the current transaction by preparing it under a GID.

        Args:
            gid_literal: The GID, as a quoted string literal.

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_commit_prepared_sql(self, gid_literal: str) -> str:
        """Returns the statement committing the transaction prepared under a GID.

        Args:
            gid_literal: The GID, as a quoted string literal.

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_rollback_prepared_sql(self, gid_literal: str) -> str:
        """Returns the statement rolling back the transaction prepared under a GID.

        Args:
            gid_literal: The GID, as a quoted string literal.

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_prepared_lookup_sql(self) -> str:
        """Returns the query finding a transaction prepared in the current database under a GID,
        bound as its one parameter - a row when there is one, none otherwise.

        Returns:
            The query.
        """

    @abc.abstractmethod
    def get_prepared_listing_sql(self, with_age_limit: bool) -> str:
        """Returns the query listing the GIDs (column ``gid``) of the transactions prepared in the
        current database whose GID matches a ``LIKE`` pattern, bound as the first parameter.

        Args:
            with_age_limit: Whether only transactions prepared more than a number of seconds ago,
                bound as the second parameter, are listed.

        Returns:
            The query.
        """

    @abc.abstractmethod
    def get_decisions_table_sql(self, table: str) -> str:
        """Returns the statement creating the decision log table unless it exists: ``xid`` text
        primary key, ``coordinator_alias`` and ``participant_aliases`` (a JSON array) text,
        ``created_at`` defaulting to the current time, and a nullable ``resolved_at``.

        Args:
            table: The decision log table - in the connection's shared schema
                (``DatabaseClient.get_shared_table_name()``).

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_decision_insert_sql(self, table: str) -> str:
        """Returns the statement recording a decision to commit: the xid, the coordinator alias and
        the JSON array of participant aliases, bound in that order.

        Args:
            table: The decision log table - in the connection's shared schema
                (``DatabaseClient.get_shared_table_name()``).

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_finish_decision_sql(self, table: str) -> str:
        """Returns the statement marking a decision resolved at the current time - every
        participant committed. The xid is bound as its one parameter.

        Args:
            table: The decision log table - in the connection's shared schema
                (``DatabaseClient.get_shared_table_name()``).

        Returns:
            The statement.
        """

    @abc.abstractmethod
    def get_pending_decisions_sql(self, table: str, with_age_limit: bool) -> str:
        """Returns every pending decision: columns ``xid`` and ``participant_aliases``.

        Young decisions must remain in the result: they protect their prepared participants
        from being mistaken for transactions with no commit decision.

        Args:
            table: The decision log table - in the connection's shared schema
                (``DatabaseClient.get_shared_table_name()``).
            with_age_limit: Include an ``is_stale`` boolean column indicating whether the
                decision was recorded more than the bound number of seconds ago. This marks
                eligibility for recovery without filtering out younger decisions.

        Returns:
            The query.
        """

    def is_prepare_conflict(self, error: BaseException) -> bool:
        """Whether PREPARE was refused because another transaction already owns its identifier.

        Cleanup must not finish that other transaction. An unknown outcome still requires
        checking for a prepared transaction left by the interrupted operation.
        """
        return False

    @abc.abstractmethod
    def get_prepare_failure(self, error: OperationalError, connection_alias: str) -> ConfigurationError | None:
        """Returns the error explaining a failed prepare the database's configuration causes -
        prepared transactions switched off, or every slot for them taken.

        Args:
            error: The error the prepare failed with.
            connection_alias: The participant's connection alias.

        Returns:
            The error to raise instead, None when ``error`` is none of those.
        """
