"""Renders a TableInfo as model source. The database's types don't map 1:1 onto fields - review the
result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.inspectdb.model_source_builder import ModelSourceBuilder

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.inspectdb.types.table_info import TableInfo


class ModelSourceGenerator:
    """Turns a TableInfo into Python model source."""

    @staticmethod
    def generate_model_source(
        table: TableInfo,
        dialect: str,
        app_label: str = "models",
        fk_target_overrides: dict[str, str] | None = None,
        skip_m2m_through_tables: bool = True,
        class_name: str | None = None,
    ) -> str:
        """Renders one table's Python model source.

        Args:
            table: The table to render.
            dialect: The backend dialect, for type mapping.
            app_label: App label a generated FK/composite-FK's target class reference is
                qualified with (``"<app_label>.<TargetClass>"``) - defaults to "models", the
                placeholder ``hare inspectdb`` itself expects a project to relocate as needed.
            fk_target_overrides: Per-target-table override of the whole ``"<app_label>.
                <TargetClass>"`` reference (keyed by the target's raw table name), for pointing an
                FK at an already-registered model whose class name needn't match
                ``ModelNaming.get_class_name()``.
            skip_m2m_through_tables: Whether a table shaped exactly like hare-orm's own
                ManyToManyField through table is skipped with a NOTE comment (the default) or
                rendered as a real model instead.
            class_name: The generated class name - defaults to ``ModelNaming.get_class_name()``.

        Returns:
            The source.
        """
        return ModelSourceBuilder(
            table, dialect, app_label, fk_target_overrides, skip_m2m_through_tables, class_name
        ).build()
