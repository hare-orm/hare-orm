from __future__ import annotations

import builtins
import itertools
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import UnSupportedError
from hare.sql.context import SqlContext
from hare.sql.enums import JoinType, Order, SetOperation
from hare.sql.exceptions import JoinException, QueryException
from hare.sql.queries.joins.join import Join
from hare.sql.queries.joins.joiner import Joiner
from hare.sql.queries.tables.aliased_query import AliasedQuery
from hare.sql.queries.tables.cte import Cte
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.queries.tables.table import Table
from hare.sql.terms.arithmetic.arithmetic_expression import ArithmeticExpression
from hare.sql.terms.base.parameterizer import Parameterizer
from hare.sql.terms.base.select_reference import SelectReference
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.function import Function
from hare.sql.terms.star import Star
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.builder.set_operation_query import SetOperationQuery
from hare.sql.queries.builder.pagination_sql_mixin import PaginationSqlMixin
from hare.sql.queries.builder.query import Query


class QueryBuilder(PaginationSqlMixin, Selectable, Term):
    """
    Query Builder is the main class in hare.sql which stores the state of a query and offers functions which allow the
    state to be branched immutably.
    """

    QUERY_CLS = Query

    #: The mutable containers a builder method may change in place. A copy shares them with its
    #: source until _mutable() gives it its own on the first change.
    COW_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {
            "_from",
            "_with",
            "_selects",
            "_columns",
            "_values",
            "_groupbys",
            "_orderbys",
            "_joins",
            "_updates",
            "_select_star_tables",
            "_on_conflict_fields",
            "_on_conflict_do_updates",
            "_returns",
            "_distinct_on",
        }
    )

    def __init__(
        self,
        wrap_set_operation_queries: bool = True,
        wrapper_cls: type[ValueWrapper] = ValueWrapper,
        immutable: bool = True,
    ) -> None:
        super().__init__(None)
        self._shared_containers: frozenset[str] = frozenset()

        self._from: list[Table] = []
        self._insert_table: Table | None = None
        self._update_table: Table | None = None
        self._delete_from = False

        self._with: list[Cte] = []
        self._selects: list[Field | Function] = []
        self._columns: list[Field] = []
        self._values: list[list[Term]] = []
        self._default_values = False
        self._distinct = False
        self._distinct_on: list[Field | Term] = []

        self._for_update = False
        self._for_update_nowait = False
        self._for_update_skip_locked = False
        self._for_update_of: set[str] = set()
        self._for_update_no_key = False

        self._wheres: QueryBuilder | Term | None = None
        self._groupbys: list[Term] = []
        self._havings: Criterion | None = None
        self._orderbys: list[tuple[Term, Order | None]] = []
        self._joins: list[Join] = []

        self._limit: ValueWrapper | None = None
        self._offset: ValueWrapper | None = None

        self._updates: list[tuple[Field, Term]] = []

        self._select_star = False
        self._select_star_tables: set[Table] = set()
        self._select_into = False

        self._subquery_count = 0
        self._foreign_table = False

        self.wrap_set_operation_queries = wrap_set_operation_queries

        self._wrapper_cls = wrapper_cls

        self.immutable = immutable

        self._on_conflict = False
        self._on_conflict_fields: list[str | Term | Field | None] = []
        self._on_conflict_constraint: str | None = None
        self._on_conflict_do_nothing = False
        self._on_conflict_do_updates: list[tuple[Field, ValueWrapper | None]] = []
        self._on_conflict_wheres: Term | None = None
        self._on_conflict_do_update_wheres: Term | None = None

        self._returns: list[Term] = []
        self._return_star = False

    def __copy__(self) -> Self:
        newone = type(self).__new__(type(self))
        newone.__dict__.update(self.__dict__)
        # Every such container starts shared with self.
        newone._shared_containers = type(self).COW_ATTRS
        return newone

    def _mutable(self, name: str) -> Any:
        """Return `name`'s current value, privately copying it first if still shared."""
        if name in self._shared_containers:
            setattr(self, name, copy(getattr(self, name)))
            self._shared_containers = self._shared_containers - {name}
        return getattr(self, name)

    @builder
    def on_conflict(self, *target_fields: str | Term) -> Self:  # type:ignore[return]
        if not self._insert_table:
            raise QueryException("On conflict only applies to insert query")
        if self._on_conflict_constraint:
            raise QueryException("Can not use on_conflict() together with on_conflict_constraint()")

        self._on_conflict = True

        for target_field in target_fields:
            if isinstance(target_field, str):
                self._mutable("_on_conflict_fields").append(self._conflict_field_str(target_field))
            elif isinstance(target_field, Term):
                self._mutable("_on_conflict_fields").append(target_field)

    @builder
    def on_conflict_constraint(self, name: str) -> Self:  # type:ignore[return]
        """``ON CONFLICT ON CONSTRAINT <name>`` - a named constraint as the conflict target, instead of
        ``on_conflict()``'s column list; one or the other.
        """
        if not self._insert_table:
            raise QueryException("On conflict only applies to insert query")
        if self._on_conflict_fields:
            raise QueryException("Can not use on_conflict_constraint() together with on_conflict(*target_fields)")

        self._on_conflict = True
        self._on_conflict_constraint = name

    @builder
    def do_update(self, update_field: str | Field, update_value: Any | None = None) -> Self:  # type:ignore[return]
        if self._on_conflict_do_nothing:
            raise QueryException("Can not have two conflict handlers")

        if isinstance(update_field, str):
            field = self._conflict_field_str(update_field)
        elif isinstance(update_field, Field):
            field = update_field
        else:
            raise QueryException("Unsupported update_field")

        if update_value is not None:
            # Term.wrap_constant() - an expression (a lock field bump) passes through, a literal is
            # wrapped.
            self._mutable("_on_conflict_do_updates").append((field, Term.wrap_constant(update_value)))
        else:
            self._mutable("_on_conflict_do_updates").append((field, None))

    def _conflict_field_str(self, term: str) -> Field | None:
        if self._insert_table:
            return Field(term, table=self._insert_table)
        return None

    def _on_conflict_sql(self, ctx: SqlContext) -> str:
        if not self._on_conflict_do_nothing and len(self._on_conflict_do_updates) == 0:
            if not self._on_conflict_fields and not self._on_conflict_constraint:
                return ""
            raise QueryException("No handler defined for on conflict")

        if self._on_conflict_do_updates and not self._on_conflict_fields and not self._on_conflict_constraint:
            raise QueryException("Can not have fieldless on conflict do update")

        conflict_query = " ON CONFLICT"
        if self._on_conflict_constraint:
            conflict_query += f" ON CONSTRAINT {ctx.quote(self._on_conflict_constraint)}"
        elif self._on_conflict_fields:
            on_conflict_ctx = ctx.copy(with_alias=True)
            fields = [
                f.get_sql(on_conflict_ctx)  # type:ignore[union-attr]
                for f in self._on_conflict_fields
            ]
            conflict_query += " (" + ", ".join(fields) + ")"

        if self._on_conflict_wheres:
            if self._on_conflict_constraint:
                raise QueryException("Can not use a WHERE index predicate with ON CONSTRAINT")
            where_ctx = ctx.copy(subquery=True)
            conflict_query += f" WHERE {self._on_conflict_wheres.get_sql(where_ctx)}"

        return conflict_query

    def _on_conflict_action_sql(self, ctx: SqlContext) -> str:
        ctx = ctx.copy(with_namespace=False)
        if self._on_conflict_do_nothing:
            return " DO NOTHING"
        elif len(self._on_conflict_do_updates) > 0:
            updates = []
            value_ctx = ctx.copy(with_namespace=True)
            for field, value in self._on_conflict_do_updates:
                if value:
                    updates.append(f"{field.get_sql(ctx)}={value.get_sql(value_ctx)}")
                else:
                    updates.append(f"{field.get_sql(ctx)}=EXCLUDED.{field.get_sql(ctx)}")
            action_sql = " DO UPDATE SET {updates}".format(updates=",".join(updates))  # nosec:B608

            if self._on_conflict_do_update_wheres:
                action_sql += " WHERE {where}".format(
                    where=self._on_conflict_do_update_wheres.get_sql(ctx.copy(subquery=True, with_namespace=True))
                )
            return action_sql

        return ""

    @builder
    def from_(self, selectable: Selectable | Query | str) -> Self:  # type:ignore[return]
        """Adds a table to the query.

        Can only be called once; raises AttributeError if called a second time.

        Args:
            selectable: A ``Table``, ``Query``, or ``str``. When a ``str`` is passed, a table
                with the name matching the ``str`` value is used.

        Returns:
            A copy of the query with the table added.
        """
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        self._mutable("_from").append(Table(selectable) if isinstance(selectable, str) else selectable)

        if isinstance(selectable, (QueryBuilder, SetOperationQuery)) and selectable.alias is None:
            if isinstance(selectable, QueryBuilder):
                sub_query_count = selectable._subquery_count
            else:
                sub_query_count = 0

            sub_query_count = max(self._subquery_count, sub_query_count)
            selectable.alias = f"sq{sub_query_count}"
            self._subquery_count = sub_query_count + 1

    @builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:  # type:ignore[return]
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table instance to be replaced.
            new_table: The table instance to replace with.

        Returns:
            A copy of the query with the tables replaced.
        """
        self._from = [
            new_table if table == current_table else table  # type:ignore[misc]
            for table in self._from
        ]
        if self._insert_table == current_table:
            self._insert_table = new_table
        if self._update_table == current_table:
            self._update_table = new_table

        self._with = [alias_query.replace_table(current_table, new_table) for alias_query in self._with]
        self._selects = [select.replace_table(current_table, new_table) for select in self._selects]
        self._columns = [column.replace_table(current_table, new_table) for column in self._columns]
        self._values = [
            [value.replace_table(current_table, new_table) for value in value_list] for value_list in self._values
        ]

        self._wheres = self._wheres.replace_table(current_table, new_table) if self._wheres else None
        self._groupbys = [groupby.replace_table(current_table, new_table) for groupby in self._groupbys]
        self._havings = self._havings.replace_table(current_table, new_table) if self._havings else None
        self._orderbys = [
            (orderby[0].replace_table(current_table, new_table), orderby[1]) for orderby in self._orderbys
        ]
        self._joins = [join.replace_table(current_table, new_table) for join in self._joins]

        if current_table in self._select_star_tables:
            select_star_tables = self._mutable("_select_star_tables")
            select_star_tables.remove(current_table)
            select_star_tables.add(cast("Table", new_table))

    @builder
    def with_(self, selectable: QueryBuilder, name: str, *terms: Term) -> Self:  # type:ignore[return]
        t = Cte(name, selectable, *terms)
        self._mutable("_with").append(t)

    @builder
    def into(self, table: str | Table) -> Self:  # type:ignore[return]
        if self._insert_table is not None:
            raise AttributeError("'Query' object has no attribute 'into'")

        if self._selects:
            self._select_into = True

        self._insert_table = table if isinstance(table, Table) else Table(table)

    @builder
    def select(self, *terms: Any) -> Self:  # type:ignore[return]
        for term in terms:
            if isinstance(term, Field):
                self._select_field(term)
            elif isinstance(term, str):
                self._select_field_str(term)
            elif isinstance(term, (Function, ArithmeticExpression)):
                self._select_other(term)  # type:ignore[arg-type]
            else:
                self._select_other(
                    self.wrap_constant(term, wrapper_cls=self._wrapper_cls)  # type:ignore[arg-type]
                )

    @builder
    def delete(self) -> Self:  # type:ignore[return]
        if self._delete_from or self._selects or self._update_table:
            raise AttributeError("'Query' object has no attribute 'delete'")

        self._delete_from = True

    @builder
    def update(self, table: str | Table) -> Self:  # type:ignore[return]
        if self._update_table is not None or self._selects or self._delete_from:
            raise AttributeError("'Query' object has no attribute 'update'")

        self._update_table = table if isinstance(table, Table) else Table(table)

    @builder
    def columns(self, *terms: Any) -> Self:  # type:ignore[return]
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute 'insert'")
        if self._default_values:
            raise QueryException("Can not use columns with default_values")

        if terms and isinstance(terms[0], (list, tuple)):
            terms = terms[0]  # type:ignore[assignment]

        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._insert_table)
            self._mutable("_columns").append(term)

    @builder
    def insert(self, *terms: Any) -> Self:  # type:ignore[return]
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute 'insert'")
        if self._default_values:
            raise QueryException("Can not use insert with default_values")

        if terms:
            self._validate_terms_and_append(*terms)

    @builder
    def default_values(self) -> Self:  # type:ignore[return]
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute 'insert'")
        if self._columns or self._values:
            raise QueryException("Can not use default_values with columns or insert")
        self._default_values = True

    @builder
    def distinct(self) -> Self:  # type:ignore[return]
        self._distinct = True

    @builder
    def distinct_on(self, *fields: str | Term) -> Self:  # type:ignore[return]
        for field in fields:
            if isinstance(field, str):
                self._mutable("_distinct_on").append(Field(field))
            elif isinstance(field, Term):
                self._mutable("_distinct_on").append(field)

    @builder
    def for_update(  # type:ignore[return]
        self,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
        no_key: bool = False,
    ) -> Self:
        if nowait and skip_locked:
            raise QueryException("for_update() options nowait and skip_locked are mutually exclusive")
        self._for_update = True
        self._for_update_skip_locked = skip_locked
        self._for_update_nowait = nowait
        self._for_update_of = set(of)
        self._for_update_no_key = no_key

    @builder
    def do_nothing(self) -> Self:  # type:ignore[return]
        if len(self._on_conflict_do_updates) > 0:
            raise QueryException("Can not have two conflict handlers")
        self._on_conflict_do_nothing = True

    @builder
    def returning(self, *terms: Any) -> Self:  # type:ignore[return]
        for term in terms:
            if isinstance(term, Field):
                self._return_field(term)
            elif isinstance(term, str):
                self._return_field_str(term)
            elif isinstance(term, ArithmeticExpression):
                self._return_other(term)
            elif isinstance(term, Function):
                raise QueryException("Aggregate functions are not allowed in returning")
            else:
                self._return_other(self.wrap_constant(term, self._wrapper_cls))

    def _validate_returning_term(self, term: Term) -> None:
        # join_tables/table_not_base_or_join don't depend on `field` - computed lazily on the
        # first iteration (preserving the original behavior of only validating when term has
        # at least one field) and reused for the rest instead of recomputing per field.
        table_not_base_or_join: bool | None = None
        for field in term.fields_():
            if table_not_base_or_join is None:
                if not any([self._insert_table, self._update_table, self._delete_from]):
                    raise QueryException("Returning can't be used in this query")
                join_tables = set(
                    itertools.chain.from_iterable(
                        [j.criterion.tables_ for j in self._joins]  # type:ignore[attr-defined]
                    )
                )
                join_and_base_tables = set(self._from) | join_tables
                table_not_base_or_join = bool(term.tables_ - join_and_base_tables)

            table_is_insert_or_update_table = field.table in {
                self._insert_table,
                self._update_table,
            }
            if not table_is_insert_or_update_table and table_not_base_or_join:
                raise QueryException("You can't return from other tables")

    def _set_returns_for_star(self) -> None:
        self._returns = [returning for returning in self._returns if not hasattr(returning, "table")]
        self._return_star = True

    def _return_field(self, term: str | Field) -> None:
        if self._return_star:
            # Do not add select terms after a star is selected
            return

        self._validate_returning_term(term)  # type:ignore[arg-type]

        if isinstance(term, Star):
            self._set_returns_for_star()

        self._mutable("_returns").append(term)

    def _return_field_str(self, term: str | Field) -> None:
        if term == "*":
            self._set_returns_for_star()
            self._mutable("_returns").append(Star())
            return

        if self._insert_table:
            table = self._insert_table
        elif self._update_table:
            table = self._update_table
        elif self._delete_from:
            table = self._from[0]
        else:
            raise QueryException("Returning can't be used in this query")
        self._return_field(Field(term, table=table))  # type:ignore[arg-type]

    def _return_other(self, function: Term) -> None:
        self._validate_returning_term(function)
        self._mutable("_returns").append(function)

    def _returning_sql(self, ctx: SqlContext) -> str:
        returning_ctx = ctx.copy(with_alias=True)
        return " RETURNING {returning}".format(
            returning=",".join(term.get_sql(returning_ctx) for term in self._returns),
        )

    @builder
    def where(self, criterion: Term | EmptyCriterion) -> Self:  # type:ignore[return]
        if isinstance(criterion, EmptyCriterion):
            return  # type:ignore[return-value]
        if not self._on_conflict:
            if not self._validate_table(criterion):
                self._foreign_table = True
            if self._wheres:
                self._wheres &= criterion  # type:ignore[operator]
            else:
                self._wheres = criterion
        else:
            if self._on_conflict_do_nothing:
                raise QueryException("DO NOTHING does not support WHERE")
            if (self._on_conflict_fields or self._on_conflict_constraint) and self._on_conflict_do_updates:
                if self._on_conflict_do_update_wheres:
                    self._on_conflict_do_update_wheres &= criterion  # type:ignore[operator]
                else:
                    self._on_conflict_do_update_wheres = criterion
            elif self._on_conflict_fields:
                if self._on_conflict_wheres:
                    self._on_conflict_wheres &= criterion  # type:ignore[operator]
                else:
                    self._on_conflict_wheres = criterion
            else:
                raise QueryException("Can not have fieldless ON CONFLICT WHERE")

    @builder
    def having(self, criterion: Criterion) -> Self:  # type:ignore[return]
        if self._havings:
            self._havings &= criterion
        else:
            self._havings = criterion

    @builder
    def groupby(self, *terms: str | int | Term) -> Self:  # type:ignore[return]
        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._from[0])
            elif isinstance(term, int):
                field = Field(str(term), table=self._from[0])
                term = field.wrap_constant(term)

            self._mutable("_groupbys").append(term)

    @builder
    def orderby(self, *fields: Any, **kwargs: Any) -> Self:  # type:ignore[return]
        for field in fields:
            field = Field(field, table=self._from[0]) if isinstance(field, str) else self.wrap_constant(field)

            self._mutable("_orderbys").append((field, kwargs.get("order")))

    @builder
    def join(
        self,
        item: Table | QueryBuilder | AliasedQuery | Selectable,
        how: JoinType = JoinType.INNER,
    ) -> Joiner:
        if isinstance(item, Table):
            return Joiner(self, item, how, type_label="table")

        elif isinstance(item, QueryBuilder):
            if item.alias is None:
                self._tag_subquery(item)
            return Joiner(self, item, how, type_label="subquery")

        elif isinstance(item, AliasedQuery):
            return Joiner(self, item, how, type_label="table")

        elif isinstance(item, Selectable):
            return Joiner(self, item, how, type_label="subquery")

        raise JoinException(f"Cannot join on type '{type(item)}'")

    @builder
    def limit(self, limit: int) -> Self:  # type:ignore[return]
        self._limit = cast("ValueWrapper", self.wrap_constant(limit))

    @builder
    def offset(self, offset: int) -> Self:  # type:ignore[return]
        self._offset = cast("ValueWrapper", self.wrap_constant(offset))

    @builder
    def union(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.UNION, wrapper_cls=self._wrapper_cls)

    @builder
    def union_all(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.UNION_ALL, wrapper_cls=self._wrapper_cls)

    @builder
    def intersect(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.INTERSECT, wrapper_cls=self._wrapper_cls)

    @builder
    def except_of(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.EXCEPT_OF, wrapper_cls=self._wrapper_cls)

    @builder
    def set(self, field: Field | str, value: Any) -> Self:  # type:ignore[return]
        field = Field(field) if not isinstance(field, Field) else field
        value = self.wrap_constant(value, wrapper_cls=self._wrapper_cls)
        self._mutable("_updates").append((field, value))

    def __add__(self, other: Self) -> SetOperationQuery:  # type:ignore[override]
        return self.union(other)

    def __mul__(self, other: Self) -> SetOperationQuery:  # type:ignore[override]
        return self.union_all(other)

    @builder
    def slice(self, slice: slice) -> Self:  # type:ignore[return]
        if slice.start is not None:
            self._offset = cast("ValueWrapper", self.wrap_constant(slice.start))
        if slice.stop is not None:
            self._limit = cast("ValueWrapper", self.wrap_constant(slice.stop))

    def __getitem__(self, item: Any) -> Self | Field:  # type:ignore[override]
        if not isinstance(item, slice):
            return super().__getitem__(item)
        return self.slice(item)

    def _select_field_str(self, term: str) -> None:
        if len(self._from) == 0:
            raise QueryException(f"Cannot select {term}, no FROM table specified.")  # nosec:B608

        if term == "*":
            self._select_star = True
            self._selects = [Star()]
            return

        self._select_field(Field(term, table=self._from[0]))

    def _select_field(self, term: Field) -> None:
        if self._select_star:
            # Do not add select terms after a star is selected
            return

        if term.table in self._select_star_tables:
            # Do not add select terms for table after a table star is selected
            return

        if isinstance(term, Star):
            self._selects = [
                select for select in self._selects if not hasattr(select, "table") or term.table != select.table
            ]
            self._mutable("_select_star_tables").add(cast("Table", term.table))

        self._mutable("_selects").append(term)

    def _select_other(self, function: Function) -> None:
        self._mutable("_selects").append(function)

    def fields_(self) -> list[Field]:  # type:ignore[override]
        # Don't return anything here. Subqueries have their own fields.
        return []

    def do_join(self, join: Join) -> None:
        base_tables = self._from + [self._update_table] + self._with
        join.validate(base_tables, self._joins)  # type:ignore[arg-type]

        # join.item in base_tables doesn't depend on the loop variable - check it once
        # instead of on every iteration of the isinstance scan (same truth value either way).
        table_in_query = join.item in base_tables and any(isinstance(clause, Table) for clause in base_tables)
        if isinstance(join.item, Table) and join.item.alias is None and table_in_query:
            # A join of the very Table object already in FROM can't be told apart in its own
            # criterion - one side must be aliased before the criterion is built.
            raise QueryException(
                "Self-join requires an explicit alias: build the joined side as "
                f"{join.item._table_name!r}.as_('...') before calling .join(...).on(...) "
                "so the ON criterion can distinguish the two occurrences of the table."
            )

        self._mutable("_joins").append(join)

    def _validate_table(self, term: Term) -> bool:
        """
        Returns False if the term references a table not already part of the
        FROM clause or JOINS and True otherwise.
        """
        base_tables = set(self._from) | {self._update_table}
        joined_tables = {join.item for join in self._joins}

        for field in term.fields_():
            table_in_base_tables = field.table in base_tables
            table_in_joins = field.table in joined_tables
            if all(
                [
                    field.table is not None,
                    not table_in_base_tables,
                    not table_in_joins,
                    field.table != self._update_table,
                ]
            ):
                return False
        return True

    def _tag_subquery(self, subquery: Self) -> None:
        subquery.alias = f"sq{self._subquery_count}"
        self._subquery_count += 1

    def _validate_terms_and_append(self, *terms: Any) -> None:
        """
        Handy function for INSERT and REPLACE statements in order to check if
        terms are introduced and how append them to `self._values`
        """
        if not isinstance(terms[0], (list, tuple, set)):
            terms = [terms]  # type:ignore[assignment]

        for values in terms:
            self._mutable("_values").append(
                [value if isinstance(value, Term) else self.wrap_constant(value) for value in values]
            )

    def __str__(self) -> str:
        return self.get_sql(self.QUERY_CLS.SQL_CONTEXT)

    def __repr__(self) -> str:
        return self.__str__()

    def __eq__(self, other: Any) -> bool:  # type:ignore[override]
        return isinstance(other, QueryBuilder) and self.alias == other.alias

    def __ne__(self, other: Any) -> bool:  # type:ignore[override]
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.alias) + sum(hash(clause) for clause in self._from)

    def get_sql(self, ctx: SqlContext | None = None) -> str:
        if not ctx:
            ctx = self.QUERY_CLS.SQL_CONTEXT
        if not self._is_renderable():
            return ""
        ctx = self._sql_context_with_namespace(ctx)

        if self._update_table:
            return self._update_sql_body(ctx)

        return self._select_insert_delete_sql(ctx)

    def _is_renderable(self) -> bool:
        if not (self._selects or self._insert_table or self._delete_from or self._update_table):
            return False
        if self._insert_table and not (self._selects or self._values or self._default_values):
            return False
        if self._update_table and not self._updates:
            return False
        return True

    def _sql_context_with_namespace(self, ctx: SqlContext) -> SqlContext:
        has_joins = bool(self._joins)
        has_multiple_from_clauses = len(self._from) > 1
        has_subquery_from_clause = len(self._from) > 0 and isinstance(self._from[0], QueryBuilder)
        has_reference_to_foreign_table = self._foreign_table
        has_update_from = self._update_table and self._from

        return ctx.copy(
            with_namespace=any(
                [
                    has_joins,
                    has_multiple_from_clauses,
                    has_subquery_from_clause,
                    has_reference_to_foreign_table,
                    has_update_from,
                ]
            )
        )

    def _update_sql_body(self, ctx: SqlContext) -> str:
        """The base (dialect-agnostic) UPDATE rendering: no self-aliasing for UPDATE-with-JOIN,
        no ORDER BY/LIMIT, no RETURNING. Dialects that support those (Postgres, SQLite) use
        _get_sql_with_self_aliased_update_and_returning() below instead."""
        querystring = self._with_sql(self._with, ctx) if self._with else ""
        querystring += self._update_sql(ctx)

        if self._joins:
            querystring += " " + " ".join(join.get_sql(ctx) for join in self._joins)

        querystring += self._set_sql(ctx)

        if self._from:
            querystring += self._from_sql(ctx)

        if self._wheres:
            querystring += self._where_sql(ctx)

        return querystring

    def _get_sql_with_self_aliased_update_and_returning(self, ctx: SqlContext | None = None) -> str:
        """Renders ``UPDATE ... FROM/JOIN ... RETURNING`` for a dialect supporting both: the target is
        aliased when there are JOINs, and RETURNING may come with ORDER BY/LIMIT.
        """
        if not ctx:
            ctx = self.QUERY_CLS.SQL_CONTEXT
        if not self._is_renderable():
            return ""
        ctx = self._sql_context_with_namespace(ctx)

        if self._update_table:
            querystring = self._with_sql(self._with, ctx) if self._with else ""
            querystring += self._update_sql(ctx)
            querystring += self._set_sql(ctx)

            # A local list - get_sql() may run several times and must not change self.
            from_selectables = list(self._from)
            if self._joins:
                from_selectables.append(self._update_table.as_(self._update_table.get_table_name() + "_"))

            if from_selectables:
                from_ctx = ctx.copy(subquery=True, with_alias=True)
                querystring += " FROM {selectable}".format(
                    selectable=",".join(clause.get_sql(from_ctx) for clause in from_selectables)
                )
            if self._joins:
                querystring += " " + " ".join(join.get_sql(ctx) for join in self._joins)

            if self._wheres:
                querystring += self._where_sql(ctx)

            if self._orderbys:
                querystring += self._orderby_sql(ctx)
            if self._limit:
                querystring += self._limit_sql(ctx)
        else:
            querystring = self._select_insert_delete_sql(ctx)

        if self._returns:
            returning_ctx = ctx.copy(with_namespace=self._update_table and self.from_)
            querystring += self._returning_sql(returning_ctx)

        return querystring

    def _select_insert_delete_sql(self, ctx: SqlContext) -> str:
        if self._delete_from:
            querystring = self._with_sql(self._with, ctx) if self._with else ""
            querystring += self._delete_sql(ctx)

        elif not self._select_into and self._insert_table:
            querystring = self._with_sql(self._with, ctx) if self._with else ""

            querystring += self._insert_sql(ctx)

            if self._columns:
                querystring += self._columns_sql(ctx)

            if self._default_values:
                querystring += self._default_values_sql(ctx)
                if self._on_conflict:
                    querystring += self._on_conflict_sql(ctx)
                    querystring += self._on_conflict_action_sql(ctx)
                return querystring

            if self._values:
                querystring += self._values_sql(ctx)
                if self._on_conflict:
                    querystring += self._on_conflict_sql(ctx)
                    querystring += self._on_conflict_action_sql(ctx)
                return querystring
            else:
                querystring += " " + self._select_sql(ctx)

        else:
            querystring = self._with_sql(self._with, ctx) if self._with else ""
            querystring += self._select_sql(ctx)

            if self._insert_table:
                querystring += self._into_sql(ctx)

        if self._from:
            querystring += self._from_sql(ctx)

        if self._joins:
            querystring += " " + " ".join(join.get_sql(ctx) for join in self._joins)

        if self._wheres:
            querystring += self._where_sql(ctx)

        # Computed once (when either clause needs it) instead of _group_sql/_orderby_sql each
        # independently rebuilding the identical {s.alias for s in self._selects} set.
        selected_aliases = {s.alias for s in self._selects} if (self._groupbys or self._orderbys) else None

        grouped_sql_by_term_id: dict[int, str] = {}
        if self._groupbys:
            querystring += self._group_sql(ctx, selected_aliases, grouped_sql_by_term_id)

        if self._havings:
            querystring += self._having_sql(ctx)

        if self._orderbys:
            querystring += self._orderby_sql(ctx, selected_aliases, grouped_sql_by_term_id)

        querystring = self._apply_pagination(querystring, ctx)

        if self._for_update:
            querystring += self._for_update_sql(ctx)

        if ctx.subquery:
            querystring = f"({querystring})"
        if self._on_conflict:
            querystring += self._on_conflict_sql(ctx)
            querystring += self._on_conflict_action_sql(ctx)
        if ctx.with_alias:
            return ctx.format_alias_sql(querystring, self.alias)

        return querystring

    def _apply_pagination(self, querystring: str, ctx: SqlContext) -> str:
        querystring += self._limit_sql(ctx)
        querystring += self._offset_sql(ctx)
        return querystring

    @staticmethod
    def _query_references_table_name(query: Selectable | None, name: str) -> bool:
        """Whether ``query`` reads a table named ``name`` - a recursive CTE's step reads a plain
        ``Table(name)``. Every branch of a set operation is checked.
        """
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.set_operation_query import SetOperationQuery

        if isinstance(query, SetOperationQuery):
            branches = [query.base_query] + [branch for _, branch in query._set_operation]
            return any(QueryBuilder._query_references_table_name(branch, name) for branch in branches)
        if isinstance(query, QueryBuilder):
            tables = [*query._from, *(join.item for join in query._joins)]
            return any(getattr(table, "_table_name", None) == name for table in tables)
        return False

    @staticmethod
    def _with_sql(with_clauses: list[Cte], ctx: SqlContext) -> str:
        """Renders a `WITH [RECURSIVE] name AS (...), ...` clause - a staticmethod (not reading
        `self._with` directly) so `SetOperationQuery.get_sql()` can render its own hoisted CTE
        list through the exact same rendering, despite not being a `QueryBuilder` itself.

        Args:
            with_clauses: The CTEs to render, in declaration order.
            ctx: The SQL rendering context.

        Returns:
            The rendered `WITH` clause, including a trailing space.
        """
        recursive = any(QueryBuilder._query_references_table_name(with_.query, with_.alias) for with_ in with_clauses)

        as_ctx = ctx.copy(subquery=False, with_alias=False)
        return f"WITH {'RECURSIVE ' if recursive else ''}" + ",".join(
            ctx.quote_alias(clause.alias)
            + ("(" + ",".join([term.get_sql(ctx) for term in clause.terms]) + ")" if clause.terms else "")
            + " AS ("
            + clause.get_sql(as_ctx)
            + ") "
            for clause in with_clauses
        )

    def get_parameterized_sql(self, ctx: SqlContext | None = None) -> tuple[str, list[Any]]:
        """
        Returns a tuple containing the query string and a list of parameters
        """
        if not ctx:
            ctx = self.QUERY_CLS.SQL_CONTEXT

        if not ctx.parameterizer:
            ctx = ctx.copy(parameterizer=Parameterizer())

        return (
            self.get_sql(ctx),
            ctx.parameterizer.values,  # type: ignore[union-attr]
        )

    def _distinct_sql(self, ctx: SqlContext) -> str:
        if self._distinct_on:
            raise UnSupportedError(f"distinct_on() has no SQL for the {ctx.dialect} dialect")
        return "DISTINCT " if self._distinct else ""

    def _for_update_sql(self, ctx: SqlContext, lock_strength="UPDATE") -> str:
        if self._for_update:
            for_update = f" FOR {lock_strength}"
            if self._for_update_of:
                for_update += f" OF {', '.join([Table(item).get_sql(ctx) for item in sorted(self._for_update_of)])}"
            if self._for_update_nowait:
                for_update += " NOWAIT"
            elif self._for_update_skip_locked:
                for_update += " SKIP LOCKED"
        else:
            for_update = ""

        return for_update

    def _select_sql(self, ctx: SqlContext) -> str:
        select_ctx = ctx.copy(subquery=True, with_alias=True)
        return "SELECT {distinct}{select}".format(
            distinct=self._distinct_sql(ctx),
            select=",".join(term.get_sql(select_ctx) for term in self._selects),
        )

    def _insert_sql(self, ctx: SqlContext) -> str:
        table = self._insert_table.get_sql(ctx)  # type:ignore[union-attr]
        return f"INSERT INTO {table}"

    @staticmethod
    def _delete_sql(ctx: SqlContext) -> str:
        return "DELETE"

    def _update_sql(self, ctx: SqlContext) -> str:
        table = self._update_table.get_sql(ctx)  # type:ignore[union-attr]
        return f"UPDATE {table}"

    def _columns_sql(self, ctx: SqlContext) -> str:
        """SQL for the columns clause of an INSERT query."""
        # Remove from ctx, never format the column terms with namespaces since only one table can be inserted into
        ctx = ctx.copy(with_namespace=False)
        return " ({columns})".format(columns=",".join(term.get_sql(ctx) for term in self._columns))

    def _values_sql(self, ctx: SqlContext) -> str:
        values_ctx = ctx.copy(subquery=True, with_alias=True)
        return " VALUES ({values})".format(
            values="),(".join(",".join(term.get_sql(values_ctx) for term in row) for row in self._values)
        )

    @staticmethod
    def _default_values_sql(ctx: SqlContext) -> str:
        return " DEFAULT VALUES"

    def _into_sql(self, ctx: SqlContext) -> str:
        into_ctx = ctx.copy(with_alias=False)
        return f" INTO {self._insert_table.get_sql(into_ctx)}"  # type:ignore[union-attr]

    def _from_sql(self, ctx: SqlContext) -> str:
        from_ctx = ctx.copy(subquery=True, with_alias=True)
        return " FROM {selectable}".format(selectable=",".join(clause.get_sql(from_ctx) for clause in self._from))

    def _where_sql(self, ctx: SqlContext) -> str:
        where_ctx = ctx.copy(subquery=True)
        wheres = cast("QueryBuilder", self._wheres)
        return f" WHERE {wheres.get_sql(where_ctx)}"

    def _group_sql(
        self,
        ctx: SqlContext,
        # builtins.set - QueryBuilder.set() shadows the builtin name in this class body.
        selected_aliases: builtins.set[str | None] | None = None,
        grouped_sql_by_term_id: dict[int, str] | None = None,
    ) -> str:
        """Renders the GROUP BY clause. A term selected under an alias is grouped by the alias when
        ``groupby_alias`` is set.

        Args:
            selected_aliases: The selected aliases, when the caller already has them.
            grouped_sql_by_term_id: Receives each grouped term's SQL by term id, for
                ``_orderby_sql()``.
        """
        clauses = []
        if selected_aliases is None:
            selected_aliases = {s.alias for s in self._selects}
        for field in self._groupbys:
            if (
                isinstance(field, SelectReference)
                and (select_position := field.get_select_position(self._selects)) is not None
            ):
                clauses.append(str(select_position))
            elif (alias := field.alias) and alias in selected_aliases:
                if ctx.groupby_alias:
                    clauses.append(ctx.quote_alias(alias))
                else:
                    for select in self._selects:
                        if select.alias == alias:
                            clauses.append(select.get_sql(ctx))
                            break
            else:
                field_sql = field.get_sql(ctx.copy(with_alias=False, subquery=True))
                if grouped_sql_by_term_id is not None:
                    grouped_sql_by_term_id[id(field)] = field_sql
                clauses.append(field_sql)

        return " GROUP BY {groupby}".format(groupby=",".join(clauses))

    def _orderby_sql(
        self,
        ctx: SqlContext,
        # builtins.set - QueryBuilder.set() shadows the builtin name in this class body.
        selected_aliases: builtins.set[str | None] | None = None,
        grouped_sql_by_term_id: dict[int, str] | None = None,
    ) -> str:
        """Renders the ORDER BY clause. A term selected under an alias is ordered by the alias when
        ``orderby_alias`` is set.

        Args:
            selected_aliases: The selected aliases, when the caller already has them.
            grouped_sql_by_term_id: The SQL ``_group_sql()`` rendered - an ORDER BY of the same term
                reuses it, so both carry the same parameters; Postgres matches the two textually.
        """
        clauses = []
        if selected_aliases is None:
            selected_aliases = {s.alias for s in self._selects}
        # A Subquery/Exists term renders its own parentheses only in subquery context.
        term_ctx = ctx.copy(subquery=True)
        grouped_sql_by_term_id = grouped_sql_by_term_id if ctx.dialect.matches_ordering_to_grouping_by_sql else None
        for field, directionality in self._orderbys:
            if ctx.orderby_alias and field.alias and field.alias in selected_aliases:
                term = ctx.quote_alias(field.alias)
            elif grouped_sql_by_term_id and field.alias is None and id(field) in grouped_sql_by_term_id:
                term = grouped_sql_by_term_id[id(field)]
            else:
                term = ctx.dialect.get_ordering_term(field, ctx).get_sql(term_ctx)

            clauses.append(f"{term} {directionality}" if directionality is not None else term)

        return " ORDER BY {orderby}".format(orderby=",".join(clauses))

    def _having_sql(self, ctx: SqlContext) -> str:
        # subquery=True as for WHERE - a subquery in HAVING (EXISTS/scalar) needs its parentheses.
        having = self._havings.get_sql(ctx.copy(subquery=True))  # type:ignore[union-attr]
        return f" HAVING {having}"

    def _set_sql(self, ctx: SqlContext) -> str:
        field_ctx = ctx.copy(with_namespace=False)
        value_ctx = ctx.copy(subquery=True)
        return " SET {set}".format(
            set=",".join(f"{field.get_sql(field_ctx)}={value.get_sql(value_ctx)}" for field, value in self._updates)
        )
