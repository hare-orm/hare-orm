from dataclasses import dataclass

from hare.migrations.migration import Migration


@dataclass(frozen=True)
class PlanStep:
    migration: Migration
    backward: bool

    @staticmethod
    def format_steps(steps: list[PlanStep], connection_name: str) -> list[str]:
        """The plan's lines: ``+ app.migration`` to apply, ``- app.migration`` to unapply.

        Args:
            steps: The plan.
            connection_name: The connection the plan runs on.

        Returns:
            The lines, after a ``# Connection:`` header.
        """
        lines = [f"# Connection: {connection_name}"]
        for step in steps:
            prefix = "-" if step.backward else "+"
            lines.append(f"{prefix} {step.migration.app_label}.{step.migration.name}")
        return lines
