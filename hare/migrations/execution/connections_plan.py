from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.loading.graph.migration_key import MigrationKey

if TYPE_CHECKING:
    from hare.migrations.execution.executor.migration_target import MigrationTarget
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_checker import MigrationSafetyChecker


class ConnectionsPlan:
    """One plan over the migrations of every connection: a migration of one connection that depends on a
    migration of another runs after it. The steps run in the plan's order, each on the executor of its
    app's connection; the steps one after another of one connection and direction run together - under
    that connection's migration lock.

    Args:
        executors: The executor of each connection, by its alias - each migrating its own apps.
    """

    def __init__(self, executors: Mapping[str, MigrationExecutor]) -> None:
        self.executors = executors
        self.connection_by_app = {
            app_label: alias for alias, executor in executors.items() for app_label in executor.apps_config
        }

    async def get_groups(
        self, targets: Iterable[MigrationTarget] | None = None
    ) -> tuple[list[tuple[str, list[PlanStep]]], set[MigrationKey]]:
        """The plan, cut into the runs of steps of one connection and direction.

        Args:
            targets: The migrations to reach - every app's latest when None.

        Returns:
            (connection alias, its steps) in order, and the migrations applied before them - each
            connection's journal for its own apps.
        """
        applied: set[MigrationKey] = set()
        for executor in self.executors.values():
            await executor.build_graph()
            applied.update(key for key in executor.loader.applied_migrations if key.app_label in executor.apps_config)
        planner = next(iter(self.executors.values()))
        steps = planner.get_migration_plan(targets, applied, planner.loader.graph, app_labels=self.connection_by_app)
        groups: list[tuple[str, list[PlanStep]]] = []
        for step in steps:
            alias = self.connection_by_app[step.migration.app_label]
            if groups and groups[-1][0] == alias and groups[-1][1][-1].backward == step.backward:
                groups[-1][1].append(step)
            else:
                groups.append((alias, [step]))
        return groups, applied

    async def migrate(
        self,
        targets: Iterable[MigrationTarget] | None = None,
        *,
        fake: bool = False,
        dry_run: bool = False,
        reporter: Callable[[str, list[PlanStep], bool, bool], object] | None = None,
        progress: Callable[[str, str, str], object] | None = None,
    ) -> None:
        """Runs the plan.

        Args:
            targets: The migrations to reach - every app's latest when None.
            fake: Record the migrations as applied or unapplied without running them.
            dry_run: Run the operations without touching the databases or the journals.
            reporter: Called with each run of steps before it runs.
            progress: Called with each migration's start and end.
        """
        groups, applied = await self.get_groups(targets)
        for alias, steps in groups:
            executor = self.executors[alias]

            async def report_steps(run_steps: list[PlanStep], alias: str = alias) -> None:
                await self.report(reporter, alias, run_steps, fake, dry_run)

            # A tenant's schema, with a journal of its own, is migrated as far as the steps reach; the
            # steps are reported under the connection's lock, without the ones another migrate ran.
            await executor.migrate(
                executor.get_targets_of_steps(steps),
                steps=steps,
                applied=set(applied),
                fake=fake,
                dry_run=dry_run,
                progress=progress,
                report=report_steps,
            )
            for step in steps:
                key = MigrationKey(app_label=step.migration.app_label, name=step.migration.name)
                if step.backward:
                    applied.discard(key)
                else:
                    applied.add(key)
        connections_with_steps = {alias for alias, _steps in groups}
        for alias, executor in self.executors.items():
            connection_targets = self.get_targets_of_connection(executor, targets)
            if connection_targets == []:
                # The targets name no app of this connection - nothing to do on it.
                continue
            if alias not in connections_with_steps:
                await self.report(reporter, alias, [], fake, dry_run)
            # Each connection once more with no step of its own: its journal is there, a squashed
            # migration whose replaced ones are all applied is recorded, and the schema of each tenant
            # reaches the targets.
            await executor.migrate(
                connection_targets,
                steps=[],
                applied=set(applied),
                fake=fake,
                dry_run=dry_run,
                progress=progress,
            )

    @staticmethod
    async def report(
        reporter: Callable[[str, list[PlanStep], bool, bool], object] | None,
        alias: str,
        steps: list[PlanStep],
        fake: bool,
        dry_run: bool,
    ) -> None:
        """Tells the reporter the steps about to run on a connection.

        Args:
            reporter: The reporter, None for none.
            alias: The connection.
            steps: Its steps.
            fake: Whether the migrations are only recorded.
            dry_run: Whether nothing is changed.
        """
        if reporter is None:
            return
        result = reporter(alias, steps, fake, dry_run)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def get_targets_of_connection(
        executor: MigrationExecutor, targets: Iterable[MigrationTarget] | None
    ) -> list[MigrationTarget] | None:
        """The targets of a connection's own apps.

        Args:
            executor: The connection's executor.
            targets: The targets of every connection - every app's latest when None.

        Returns:
            The targets, None for each of its apps' latest.
        """
        if targets is None:
            return None
        return [target for target in targets if target.app_label in executor.apps_config]

    async def get_plan_lines(self, targets: Iterable[MigrationTarget] | None = None) -> list[str]:
        """The plan's lines, a ``# Connection:`` header before each run of steps.

        Args:
            targets: The migrations to reach - every app's latest when None.

        Returns:
            The lines.
        """
        groups, _applied = await self.get_groups(targets)
        lines = [line for alias, steps in groups for line in PlanStep.format_steps(steps, alias)]
        connections_with_steps = {alias for alias, _steps in groups}
        # A connection the targets name an app of, with nothing to do, shows its header alone.
        for alias, executor in self.executors.items():
            if alias not in connections_with_steps and self.get_targets_of_connection(executor, targets) != []:
                lines.extend(PlanStep.format_steps([], alias))
        return lines

    async def check_safety(self, checker: MigrationSafetyChecker) -> list[MigrationRisk]:
        """Checks the migrations the plan applies against the databases, each on its connection.

        Args:
            checker: The migration safety check.

        Returns:
            The risks, in the order of the plan.
        """
        groups, _applied = await self.get_groups()
        positions = {
            (step.migration.app_label, step.migration.name): position
            for position, step in enumerate(step for _alias, steps in groups for step in steps)
        }
        risks: list[MigrationRisk] = []
        for executor in self.executors.values():
            risks.extend(await executor.check_safety(checker))
        return sorted(risks, key=lambda risk: positions.get((risk.app_label, risk.migration_name), len(positions)))
