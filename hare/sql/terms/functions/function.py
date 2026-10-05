from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import UnSupportedError
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table


class Function(Criterion):
    #: Whether the function has no standard SQL of its own - rendered only by a dialect's renderer.
    requires_dialect_renderer: ClassVar[bool] = False

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(kwargs.get("alias"))
        self.name = name
        self.args: list[Term] = [self.wrap_constant(parameter) for parameter in args]
        self.schema = kwargs.get("schema")

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for arg in self.args:
            yield from arg.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        """Shortcut that assumes a function is aggregate if its single argument is aggregate.

        A more sophisticated approach is needed, however it is unclear how that might work.

        Returns:
            True if the function accepts one argument and that argument is aggregate.
        """
        return Term.get_combined_is_aggregate([arg.is_aggregate for arg in self.args])

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.args = [parameter.replace_table(current_table, new_table) for parameter in self.args]
        return self

    def get_special_parameters_sql(self, sql_context: SqlContext) -> Any:
        pass

    @staticmethod
    def get_arg_sql(arg: Any, sql_context: SqlContext) -> str:
        arg_context = sql_context.copy(with_alias=False)
        return arg.get_sql(arg_context) if hasattr(arg, "get_sql") else str(arg)

    def get_dialect_special_name(self, sql_context: SqlContext) -> str:
        name_renderer = sql_context.dialect.renderers.get_name_renderer(type(self))
        return "" if name_renderer is None else name_renderer(self, sql_context)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        # pylint: disable=E1111
        special_parameters_sql = self.get_special_parameters_sql(sql_context)

        return "{name}({args}{special})".format(
            name=self.get_dialect_special_name(sql_context) or self.name,
            args=",".join(self.get_arg_sql(arg, sql_context) for arg in self.args),
            special=(" " + special_parameters_sql) if special_parameters_sql else "",
        )

    def get_sql(self, sql_context: SqlContext) -> str:
        # self.name is never user-controlled - every Function subclass/call site passes a
        # hardcoded literal function name, so it needs no identifier quoting/escaping here.
        renderer = sql_context.dialect.renderers.get(
            type(self)
        ) or sql_context.dialect.renderers.get_function_renderer(self.name)
        if renderer is not None:
            function_sql = renderer(self, sql_context)
        elif self.requires_dialect_renderer:
            raise UnSupportedError(f"{type(self).__name__} has no SQL for the {sql_context.dialect} dialect")
        else:
            function_sql = self.get_function_sql(sql_context)

        if self.schema is not None:
            function_sql = f"{self.schema.get_sql(sql_context)}.{function_sql}"

        if sql_context.with_alias:
            return sql_context.format_alias_sql(function_sql, self.alias)

        return function_sql
