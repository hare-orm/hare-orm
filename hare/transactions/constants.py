#: The prefix of every GID Transactions.distributed() prepares under - recovery tells hare's
#: prepared transactions from other tools'.
DISTRIBUTED_TRANSACTION_XID_PREFIX = "hare_dtx_"

#: Length of the unique part (a uuid4 hex) ending every `Transactions.distributed()` xid.
DISTRIBUTED_TRANSACTION_XID_UNIQUE_PART_LENGTH = 32

#: The table Transactions.distributed() records its commit decisions in on the coordinator - created
#: on first use, outside migrations. participant_aliases is comma-joined text.
DISTRIBUTED_DECISIONS_TABLE_NAME = "hare_distributed_decisions"

#: `hare distributed-recover`'s default `--older-than`, in seconds - a younger entry may belong to a
#: call still running.
DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS = 300

#: Sanity ceiling (ten years) for the `distributed-recover --older-than` argument.
MAX_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS = 10 * 365 * 24 * 60 * 60

#: Smallest `Transactions.atomic(statement_timeout=...)` value (seconds) - one
#: millisecond, the unit Postgres's statement_timeout/lock_timeout settings take (0 would mean
#: "no timeout" there).
MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS = 0.001

#: Largest `Transactions.atomic(statement_timeout=...)` value (seconds) - Postgres's own
#: ceiling for statement_timeout/lock_timeout (INT_MAX milliseconds).
MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS = 2_147_483.647
