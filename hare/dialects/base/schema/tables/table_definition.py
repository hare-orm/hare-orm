from __future__ import annotations

from dataclasses import dataclass

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint


@dataclass(frozen=True, slots=True)
class TableDefinition:
    """Prepared declarations shared by initial creation and table replacement.

    Identifiers and expressions are already rendered by the dialect. Rendering the
    CREATE statement does not inspect a live model, run I/O or evaluate a Python
    default. Separate constraints and deferred references retain their own order.
    A replacement supplies its temporary table name while preparing the definition;
    it does not rewrite an already rendered CREATE statement.
    """

    qualified_name: str
    columns: tuple[str, ...]
    constraints: tuple[str, ...]
    references: frozenset[tuple[str | None, str]]
    deferred_foreign_keys: tuple[ForeignKeyConstraint, ...]
    prefix: str = ""
    exists: str = ""
    suffix: str = ""
    comment: str = ""
    placement_sql: str = ""

    def get_create_sql(self, template: str) -> str:
        """Render the same declaration with the dialect's CREATE TABLE grammar."""
        return template.format(
            prefix=self.prefix,
            exists=self.exists,
            table_name=self.qualified_name + self.placement_sql,
            fields="\n    {}\n".format(",\n    ".join(self.columns)),
            comment=self.comment,
            extra=self.suffix,
        )
