"""Console output formatting for the `hare` CLI's migration-related commands."""

from __future__ import annotations

from typing import Any

from hare.cli.output.terminal_colors import TerminalColors
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader


class OutputFormatter:
    """Formats migration state (history/heads/plans/progress) as colored console output."""

    @staticmethod
    def echo_connection_header(connection_alias: str, *, suffix: str = "") -> None:
        print(f"{TerminalColors.BOLD}Connection: {connection_alias}{suffix}{TerminalColors.RESET}")

    @staticmethod
    def echo_app_header(app_label: str) -> None:
        print(f"  {TerminalColors.BOLD}{app_label}:{TerminalColors.RESET}")

    @staticmethod
    def emit_history(
        applied: list[MigrationKey],
        connection_alias: str,
        apps_config: dict[str, dict[str, Any]],
    ) -> None:
        by_app: dict[str, list[str]] = {label: [] for label in apps_config}
        for key in applied:
            if key.app_label in by_app:
                by_app[key.app_label].append(key.name)
        OutputFormatter.echo_connection_header(connection_alias)
        for app_label in sorted(by_app):
            OutputFormatter.echo_app_header(app_label)
            names = by_app[app_label]
            if not names:
                print(f"    {TerminalColors.DIM}(no applied migrations){TerminalColors.RESET}")
                continue
            for name in names:
                print(f"    {TerminalColors.GREEN}-{TerminalColors.RESET} {app_label} {name}")

    @staticmethod
    def emit_heads(
        loader: MigrationLoader,
        connection_alias: str,
        apps_config: dict[str, dict[str, Any]],
    ) -> None:
        OutputFormatter.echo_connection_header(connection_alias)
        for app_label in sorted(apps_config):
            OutputFormatter.echo_app_header(app_label)
            keys = list(loader.graph.leaf_nodes(app_label))
            if not keys:
                print(f"    {TerminalColors.DIM}(no heads){TerminalColors.RESET}")
                continue
            for key in keys:
                print(f"    {TerminalColors.CYAN}-{TerminalColors.RESET} {app_label}.{key.name}")

    @staticmethod
    def emit_migration_plan(
        connection_alias: str,
        plan: list[PlanStep],
        fake: bool,
        dry_run: bool,
    ) -> None:
        suffixes = []
        if dry_run:
            suffixes.append("dry-run")
        if fake:
            suffixes.append("fake")
        suffix = f" ({', '.join(suffixes)})" if suffixes else ""
        OutputFormatter.echo_connection_header(connection_alias, suffix=suffix)
        if not plan:
            print(f"  {TerminalColors.DIM}No migrations to apply{TerminalColors.RESET}")
            return
        applied = 0
        rolled_back = 0
        for step in plan:
            label = f"{step.migration.app_label}.{step.migration.name}"
            if step.backward:
                rolled_back += 1
                print(f"  {TerminalColors.YELLOW}ROLLBACK{TerminalColors.RESET}  {label}")
            else:
                applied += 1
                print(f"  {TerminalColors.CYAN}APPLY{TerminalColors.RESET}     {label}")
        print(f"  {TerminalColors.DIM}Plan: {applied} to apply, {rolled_back} to roll back{TerminalColors.RESET}")

    #: Whether the last progress line was started but not yet finished with OK/FAILED.
    progress_line_open = False

    @staticmethod
    def progress_reporter(event: str, app_label: str, name: str) -> None:
        """Inline progress reporter for migration execution."""
        label = f"{app_label}.{name}"
        if event == "apply_start":
            print(f"  Applying {TerminalColors.CYAN}{label}{TerminalColors.RESET}...", end="", flush=True)
            OutputFormatter.progress_line_open = True
        elif event == "apply_done":
            print(f" {TerminalColors.GREEN}OK{TerminalColors.RESET}")
            OutputFormatter.progress_line_open = False
        elif event == "rollback_start":
            print(f"  Rolling back {TerminalColors.YELLOW}{label}{TerminalColors.RESET}...", end="", flush=True)
            OutputFormatter.progress_line_open = True
        elif event == "rollback_done":
            print(f" {TerminalColors.GREEN}OK{TerminalColors.RESET}")
            OutputFormatter.progress_line_open = False

    @staticmethod
    def dry_run_progress_reporter(event: str, app_label: str, name: str) -> None:
        """Progress reporter for a dry run: nothing is executed, so only the plan is echoed."""
        label = f"{app_label}.{name}"
        if event == "apply_done":
            print(f"  Would apply {TerminalColors.CYAN}{label}{TerminalColors.RESET}")
        elif event == "rollback_done":
            print(f"  Would roll back {TerminalColors.YELLOW}{label}{TerminalColors.RESET}")

    @staticmethod
    def finish_open_progress_line() -> None:
        """Terminates a progress line left unfinished by a failing migration step."""
        if OutputFormatter.progress_line_open:
            print(f" {TerminalColors.RED}FAILED{TerminalColors.RESET}", flush=True)
            OutputFormatter.progress_line_open = False
