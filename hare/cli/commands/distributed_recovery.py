"""The `hare` CLI's `distributed-recover` command implementation."""

import argparse

from hare.cli.colors import TerminalColors
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIUsageError
from hare.core.connections import Connections
from hare.transactions.distributed.distributed_coordinator import DistributedCoordinator
from hare.transactions.distributed.stale_prepared_transaction import StalePreparedTransaction
from hare.transactions.enums import DistributedTransactionResolution


class DistributedRecoveryCommands:
    """Implements `distributed-recover`, sharing `CommandContext`'s config loading/error-boundary
    infrastructure with the CLI's other command classes."""

    @staticmethod
    async def distributed_recover(
        ctx: CLIContext, coordinator: str, finish: bool, older_than_seconds: int
    ) -> int | None:
        """Reports - and with ``finish``, resolves - the prepared transactions
        ``Transactions.distributed()`` left pending.

        Returns:
            1 if a stale prepared transaction was found, or is left pending after ``--finish``; None
            otherwise.
        """
        hare_config = CommandContext.load_config(ctx)
        if coordinator not in hare_config.connections:
            raise CLIUsageError(f"Unknown connection alias: {coordinator}")
        config_dict = hare_config.to_dict()

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict):
            stale = await DistributedCoordinator.detect_stale_prepared_transactions(coordinator, older_than_seconds)
            if not stale:
                print(f"{TerminalColors.BOLD}No stale distributed transactions found.{TerminalColors.RESET}")
                return None

            if not finish:
                for entry in stale:
                    print(
                        f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} "
                        f"needs {entry.resolution.upper()} PREPARED - {entry.reason}"
                    )
                return 1

            pending_entries: list[StalePreparedTransaction] = []
            for entry in stale:
                try:
                    connection = Connections.get(entry.participant_alias)
                    if entry.resolution == DistributedTransactionResolution.COMMIT:
                        await DistributedCoordinator.commit_prepared(connection, entry.participant_alias, entry.xid)
                    else:
                        await DistributedCoordinator.rollback_prepared(connection, entry.participant_alias, entry.xid)
                except Exception as exc:
                    print(
                        f"  {TerminalColors.RED}!{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} failed: {exc}"
                    )
                    pending_entries.append(entry)
                else:
                    print(
                        f"  {TerminalColors.GREEN}+{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} "
                        f"resolved ({entry.resolution})"
                    )

            # An xid is resolved only once every entry of it succeeded - a later run would otherwise
            # take a pending participant for an orphan and roll back a committed write.
            pending_xids = {entry.xid for entry in pending_entries}
            finished_xids = {entry.xid for entry in stale} - pending_xids
            coordinator_connection = Connections.get(coordinator)
            for xid in finished_xids:
                await DistributedCoordinator.mark_decision_resolved(coordinator_connection, xid)
            return 1 if pending_entries else None

    @staticmethod
    async def _run_distributed_recover(ctx: CLIContext, args: argparse.Namespace) -> int | None:
        return await DistributedRecoveryCommands.distributed_recover(
            ctx, args.coordinator, args.finish, args.older_than_seconds
        )
