from __future__ import annotations

from dataclasses import dataclass, field

from hare.models import Model


@dataclass
class ModelSqlData:
    """The DDL creating one model.

    Attributes:
        table_key: The ``(schema, table)`` of the model's table.
        model: The model.
        table_sql: The statements creating the table with its indexes and comments.
        constraint_sqls: The statements adding the table's ``Meta.constraints`` once it exists,
            each on its own and without a closing semicolon - the ones a dialect writes into
            ``CREATE TABLE`` are in ``table_sql``.
        references: The ``(schema, table)`` of every table a foreign key constraint of the table
            points at - they have to exist first.
        many_to_many_tables_sql: The statements creating each automatic through table of the model's
            M2M fields.
        deferred_foreign_key_sqls: The statements adding the foreign keys to tables created after this
            one - of a table in a cycle of foreign keys - each closed with a semicolon.
    """

    table_key: tuple[str | None, str]
    model: type[Model]
    table_sql: str
    constraint_sqls: list[str]
    references: set[tuple[str | None, str]]
    many_to_many_tables_sql: list[str]
    deferred_foreign_key_sqls: list[str] = field(default_factory=list)

    def get_table_creation_sql(self) -> str:
        """Returns the statements creating the table with everything on it, as one script.

        Returns:
            The table's statements followed by its constraints', each closed with a semicolon.
        """
        return "\n".join([self.table_sql, *(f"{constraint_sql};" for constraint_sql in self.constraint_sqls)])
