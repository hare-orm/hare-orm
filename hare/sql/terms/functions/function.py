from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import UnSupportedError
from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table


class Function(Criterion):
    #: Whether the function has no standard SQL of its own - rendered only by a dialect's renderer.
    requires_dialect_renderer: ClassVar[bool] = False

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(kwargs.get("alias"))
        self.name = name
        self.args: list[Term] = [self.wrap_constant(param) for param in args]
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

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.args = [param.replace_table(current_table, new_table) for param in self.args]

    def get_special_params_sql(self, ctx: SqlContext) -> Any:
        pass

    @staticmethod
    def get_arg_sql(arg: Any, ctx: SqlContext) -> str:
        arg_ctx = ctx.copy(with_alias=False)
        return arg.get_sql(arg_ctx) if hasattr(arg, "get_sql") else str(arg)

    def get_dialect_special_name(self, ctx: SqlContext) -> str:
        name_renderer = ctx.dialect.renderers.get_name_renderer(type(self))
        return "" if name_renderer is None else name_renderer(self, ctx)

    def get_function_sql(self, ctx: SqlContext) -> str:
        # pylint: disable=E1111
        special_params_sql = self.get_special_params_sql(ctx)

        return "{name}({args}{special})".format(
            name=self.get_dialect_special_name(ctx) or self.name,
            args=",".join(self.get_arg_sql(arg, ctx) for arg in self.args),
            special=(" " + special_params_sql) if special_params_sql else "",
        )

    def get_sql(self, ctx: SqlContext) -> str:
        # self.name is never user-controlled - every Function subclass/call site passes a
        # hardcoded literal function name, so it needs no identifier quoting/escaping here.
        renderer = ctx.dialect.renderers.get(type(self)) or ctx.dialect.renderers.get_function_renderer(self.name)
        if renderer is not None:
            function_sql = renderer(self, ctx)
        elif self.requires_dialect_renderer:
            raise UnSupportedError(f"{type(self).__name__} has no SQL for the {ctx.dialect} dialect")
        else:
            function_sql = self.get_function_sql(ctx)

        if self.schema is not None:
            function_sql = f"{self.schema.get_sql(ctx)}.{function_sql}"

        if ctx.with_alias:
            return ctx.format_alias_sql(function_sql, self.alias)

        return function_sql
