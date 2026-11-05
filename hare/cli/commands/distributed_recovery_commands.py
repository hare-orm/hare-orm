"""The `hare` CLI's `distributed-recover` command implementation."""

from __future__ import annotations

import argparse

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIUsageError
from hare.cli.output.terminal_colors import TerminalColors
from hare.transactions.distributed.distributed_recovery import DistributedRecovery


class DistributedRecoveryCommands:
    """Implements `distributed-recover`, sharing `CommandContext`'s config loading/error-boundary
    infrastructure with the CLI's other command classes."""

    @staticmethod
    async def distributed_recover(
        cli_context: CLIContext, coordinator: str, finish: bool, older_than_seconds: int
    ) -> int | None:
        """Reports - and with ``finish``, resolves - the prepared transactions
        ``Transactions.distributed()`` left pending.

        Returns:
            1 if a stale prepared transaction was found, or is left pending after ``--finish``; None
            otherwise.
        """
        hare_config = CommandContext.load_config(cli_context)
        if coordinator not in hare_config.connections:
            raise CLIUsageError(f"Unknown connection alias: {coordinator}")

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(hare_config):
            report = await DistributedRecovery.run(coordinator, finish=finish, older_than_seconds=older_than_seconds)
            if not report.entries and not report.decision_errors:
                print(f"{TerminalColors.BOLD}No stale distributed transactions found.{TerminalColors.RESET}")
                return None

            if not finish:
                for entry in report.entries:
                    print(
                        f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} "
                        f"needs {entry.resolution.upper()} PREPARED - {entry.reason}"
                    )
                return 1

            for result in report.attempts:
                entry = result.entry
                if result.error is not None:
                    print(
                        f"  {TerminalColors.RED}!{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} failed: {result.error}"
                    )
                else:
                    print(
                        f"  {TerminalColors.GREEN}+{TerminalColors.RESET} xid={entry.xid} "
                        f"alias={entry.participant_alias} "
                        f"resolved ({entry.resolution})"
                    )

            for xid, error in report.decision_errors:
                print(f"  {TerminalColors.RED}!{TerminalColors.RESET} xid={xid} decision update failed: {error}")
            return 1 if report.decision_errors or any(result.error is not None for result in report.attempts) else None

    @staticmethod
    async def run_distributed_recover(cli_context: CLIContext, args: argparse.Namespace) -> int | None:
        return await DistributedRecoveryCommands.distributed_recover(
            cli_context, args.coordinator, args.finish, args.older_than_seconds
        )
