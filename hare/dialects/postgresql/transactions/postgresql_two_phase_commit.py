from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.transactions.two_phase_commit import TwoPhaseCommit
from hare.dialects.postgresql.transactions.constants import (
    POSTGRESQL_PREPARED_TRANSACTIONS_DISABLED_MESSAGE,
    POSTGRESQL_PREPARED_TRANSACTIONS_LIMIT_REACHED_MESSAGE,
)
from hare.exceptions import ConfigurationError
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.exceptions import OperationalError


class PostgresqlTwoPhaseCommit(TwoPhaseCommit):
    """PostgreSQL's ``PREPARE TRANSACTION``/``COMMIT PREPARED``/``ROLLBACK PREPARED``.

    Prepared transactions are listed in ``pg_prepared_xacts``, which is cluster-wide - every
    lookup is limited to the current database. They need ``max_prepared_transactions`` above
    zero on the server.
    """

    def get_prepare_sql(self, gid_literal: str) -> str:
        return f"PREPARE TRANSACTION {gid_literal}"

    def get_commit_prepared_sql(self, gid_literal: str) -> str:
        return f"COMMIT PREPARED {gid_literal}"

    def get_rollback_prepared_sql(self, gid_literal: str) -> str:
        return f"ROLLBACK PREPARED {gid_literal}"

    def get_prepared_lookup_sql(self) -> str:
        return "SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1 AND database = current_database()"

    def get_prepared_listing_sql(self, with_age_limit: bool) -> str:
        sql = "SELECT gid FROM pg_catalog.pg_prepared_xacts WHERE gid LIKE $1 AND database = current_database()"
        if with_age_limit:
            sql += " AND prepared < now() - make_interval(secs => $2)"
        return sql

    def get_decisions_table_sql(self) -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS {DISTRIBUTED_DECISIONS_TABLE_NAME} ("
            "xid TEXT PRIMARY KEY, "
            "coordinator_alias TEXT NOT NULL, "
            "participant_aliases TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "resolved_at TIMESTAMPTZ)"
        )

    def get_decision_insert_sql(self) -> str:
        return (
            f"INSERT INTO {DISTRIBUTED_DECISIONS_TABLE_NAME} "  # nosec B608 - a constant table name
            "(xid, coordinator_alias, participant_aliases) VALUES ($1, $2, $3)"
        )

    def get_finish_decision_sql(self) -> str:
        return f"UPDATE {DISTRIBUTED_DECISIONS_TABLE_NAME} SET resolved_at = now() WHERE xid = $1"  # nosec B608

    def get_pending_decisions_sql(self, with_age_limit: bool) -> str:
        sql = (
            f"SELECT xid, participant_aliases FROM {DISTRIBUTED_DECISIONS_TABLE_NAME} "  # nosec B608
            "WHERE resolved_at IS NULL"
        )
        if with_age_limit:
            sql += " AND created_at < now() - make_interval(secs => $1)"
        return sql

    def get_prepare_failure(self, error: OperationalError, connection_alias: str) -> ConfigurationError | None:
        message = str(error).lower()
        if POSTGRESQL_PREPARED_TRANSACTIONS_LIMIT_REACHED_MESSAGE in message:
            return ConfigurationError(
                f"PREPARE TRANSACTION failed on {connection_alias!r} - every max_prepared_transactions slot "
                "there is taken. Prepared transactions left behind by an interrupted "
                "Transactions.distributed() hold them: run `hare distributed-recover`, and check "
                "pg_prepared_xacts for ones that are not hare's."
            )
        if POSTGRESQL_PREPARED_TRANSACTIONS_DISABLED_MESSAGE in message:
            return ConfigurationError(
                f"PREPARE TRANSACTION failed on {connection_alias!r} - PostgreSQL has "
                "max_prepared_transactions=0 there (the default on many installs). Set it "
                "to a nonzero value in postgresql.conf and restart PostgreSQL to use "
                "Transactions.distributed() on this alias."
            )
        return None
