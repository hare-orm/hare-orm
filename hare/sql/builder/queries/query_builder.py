from __future__ import annotations

import builtins
import itertools
from collections.abc import Iterable
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import UnSupportedError
from hare.sql.builder.joins.join import Join
from hare.sql.builder.joins.joiner import Joiner
from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering
from hare.sql.builder.tables.aliased_query import AliasedQuery
from hare.sql.builder.tables.cte import Cte
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder.tables.table import Table
from hare.sql.builder_methods import BuilderMethods
from hare.sql.constants import QUERY_WITHOUT_INSERT_MESSAGE
from hare.sql.enums import JoinType, Order, SetOperation
from hare.sql.exceptions import JoinException, QueryException
from hare.sql.sql_context import SqlContext
from hare.sql.terms.arithmetic_expression import ArithmeticExpression
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.function import Function
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.star import Star
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.builder.queries.pagination_sql import PaginationSql
from hare.sql.builder.queries.query import Query


class QueryBuilder(PaginationSql, Selectable, Term):
    """
    Query Builder is the main class in hare.sql which stores the state of a query and offers functions which allow the
    state to be branched immutably.
    """

    #: The mutable containers a builder method may change in place. A copy shares them with its
    #: source until _mutable() gives it its own on the first change.
    COPY_ON_WRITE_ATTRIBUTES: ClassVar[frozenset[str]] = frozenset(
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
            "_dialect_clauses",
        }
    )

    def __init__(
        self,
        query_class: type[Query] = Query,
        wrap_set_operation_queries: bool = True,
        wrapper_class: type[ValueWrapper] = ValueWrapper,
        immutable: bool = True,
    ) -> None:
        super().__init__(None)
        self._shared_containers: frozenset[str] = frozenset()
        #: The query class the builder belongs to - its SQL_CONTEXT is the dialect a builder renders
        #: in by default.
        self.query_class = query_class

        self._from: list[Selectable] = []
        self._insert_table: Table | None = None
        self._update_table: Table | None = None
        self._delete_from = False

        self._with: list[Cte] = []
        self._selects: list[Field | Function] = []
        self._columns: list[Field] = []
        self._values: list[list[Term]] = []
        self._default_values = False
        #: The SQL of the rows an INSERT writes in place of VALUES - a dialect's source of rows.
        self._rows_source_sql: str | None = None
        #: Whether an INSERT writes its rows as a SELECT of them, not as VALUES.
        self._insert_rows_by_select = False
        self._distinct = False
        self._distinct_on: list[Field | Term] = []

        self._for_update = False
        self._for_update_nowait = False
        self._for_update_skip_locked = False
        self._for_update_of: set[str] = set()
        #: The lock strength of FOR UPDATE (a ``RowLockStrength`` value) - None for FOR UPDATE itself.
        self._for_update_strength: str | None = None
        #: The sample of the first FROM table (``TABLESAMPLE``) - its method, percent and seed; None
        #: for every row.
        self._table_sample: tuple[str, float, int | None] | None = None
        #: The clauses of the dialect's own QuerySet methods (``QuerySetExtensions``), by name - only that
        #: dialect's ``QueryClauses`` reads them.
        self._dialect_clauses: dict[str, Any] = {}

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

        self._wrapper_class = wrapper_class

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
        newone._shared_containers = type(self).COPY_ON_WRITE_ATTRIBUTES
        return newone

    def _mutable(self, name: str) -> Any:
        """Return `name`'s current value, privately copying it first if still shared."""
        if name in self._shared_containers:
            setattr(self, name, copy(getattr(self, name)))
            self._shared_containers = self._shared_containers - {name}
        return getattr(self, name)

    @BuilderMethods.builder
    def set_dialect_clause(self, name: str, value: Any) -> Self:
        """Sets a clause of the dialect's own QuerySet method - its ``QueryClauses`` writes it.

        Args:
            name: The clause.
            value: What the dialect's ``QueryClauses`` writes it from.

        Returns:
            The builder with the clause.
        """
        self._mutable("_dialect_clauses")[name] = value
        return self

    @BuilderMethods.builder
    def on_conflict(self, *target_fields: str | Term) -> Self:
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
        return self

    @BuilderMethods.builder
    def on_conflict_constraint(self, name: str) -> Self:
        """``ON CONFLICT ON CONSTRAINT <name>`` - a named constraint as the conflict target, instead of
        ``on_conflict()``'s column list; one or the other.
        """
        if not self._insert_table:
            raise QueryException("On conflict only applies to insert query")
        if self._on_conflict_fields:
            raise QueryException("Can not use on_conflict_constraint() together with on_conflict(*target_fields)")

        self._on_conflict = True
        self._on_conflict_constraint = name
        return self

    @BuilderMethods.builder
    def do_update(self, update_field: str | Field, update_value: Any | None = None) -> Self:
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
        return self

    def _conflict_field_str(self, term: str) -> Field | None:
        if self._insert_table:
            return Field(term, table=self._insert_table)
        return None

    @BuilderMethods.builder
    def from_(self, selectable: Selectable | Query | str) -> Self:
        """Adds a table to the query.

        Can only be called once; raises AttributeError if called a second time.

        Args:
            selectable: A ``Table``, ``Query``, or ``str``. When a ``str`` is passed, a table
                with the name matching the ``str`` value is used.

        Returns:
            A copy of the query with the table added.
        """
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        self._mutable("_from").append(Table(selectable) if isinstance(selectable, str) else selectable)

        if isinstance(selectable, (QueryBuilder, SetOperationQuery)) and selectable.alias is None:
            sub_query_count = selectable._subquery_count if isinstance(selectable, QueryBuilder) else 0
            sub_query_count = max(self._subquery_count, sub_query_count)
            selectable.alias = f"sq{sub_query_count}"
            self._subquery_count = sub_query_count + 1
        return self

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
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
        return self

    @BuilderMethods.builder
    def with_(self, selectable: Selectable, name: str, *terms: Term) -> Self:
        cte = Cte(name, selectable, *terms)
        self._mutable("_with").append(cte)
        return self

    @BuilderMethods.builder
    def into(self, table: str | Table) -> Self:
        if self._insert_table is not None:
            raise AttributeError("'Query' object has no attribute 'into'")

        if self._selects:
            self._select_into = True

        self._insert_table = table if isinstance(table, Table) else Table(table)
        return self

    @BuilderMethods.builder
    def select(self, *terms: Any) -> Self:
        for term in terms:
            if isinstance(term, Field):
                self._select_field(term)
            elif isinstance(term, str):
                self._select_field_str(term)
            elif isinstance(term, (Function, ArithmeticExpression)):
                self._select_other(term)  # type:ignore[arg-type]
            else:
                self._select_other(
                    self.wrap_constant(term, wrapper_class=self._wrapper_class)  # type:ignore[arg-type]
                )
        return self

    @BuilderMethods.builder
    def delete(self) -> Self:
        if self._delete_from or self._selects or self._update_table:
            raise AttributeError("'Query' object has no attribute 'delete'")

        self._delete_from = True
        return self

    @BuilderMethods.builder
    def update(self, table: str | Table) -> Self:
        if self._update_table is not None or self._selects or self._delete_from:
            raise AttributeError("'Query' object has no attribute 'update'")

        self._update_table = table if isinstance(table, Table) else Table(table)
        return self

    @BuilderMethods.builder
    def columns(self, *terms: Any) -> Self:
        if self._insert_table is None:
            raise AttributeError(QUERY_WITHOUT_INSERT_MESSAGE)
        if self._default_values:
            raise QueryException("Can not use columns with default_values")

        if terms and isinstance(terms[0], (list, tuple)):
            terms = terms[0]  # type:ignore[assignment]

        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._insert_table)
            self._mutable("_columns").append(term)
        return self

    @BuilderMethods.builder
    def insert(self, *terms: Any) -> Self:
        if self._insert_table is None:
            raise AttributeError(QUERY_WITHOUT_INSERT_MESSAGE)
        if self._default_values:
            raise QueryException("Can not use insert with default_values")

        if terms:
            self._validate_terms_and_append(*terms)
        return self

    @BuilderMethods.builder
    def default_values(self) -> Self:
        if self._insert_table is None:
            raise AttributeError(QUERY_WITHOUT_INSERT_MESSAGE)
        if self._columns or self._values:
            raise QueryException("Can not use default_values with columns or insert")
        self._default_values = True
        return self

    @BuilderMethods.builder
    def insert_rows_by_select(self) -> Self:
        """Writes the rows of ``insert()`` as a ``SELECT`` of each row (``UNION ALL`` of several), not
        as ``VALUES`` - for values a dialect's ``VALUES`` doesn't read."""
        self._insert_rows_by_select = True
        return self

    @BuilderMethods.builder
    def insert_rows_from(self, rows_source_sql: str) -> Self:
        """Inserts the rows ``rows_source_sql`` gives - a dialect's source of rows - in place of
        ``VALUES``; ``ON CONFLICT`` and ``RETURNING`` apply as to ``insert()``.

        Args:
            rows_source_sql: The SQL of the rows, e.g. ``SELECT * FROM unnest(...)``.
        """
        if self._insert_table is None:
            raise AttributeError(QUERY_WITHOUT_INSERT_MESSAGE)
        if self._default_values or self._values:
            raise QueryException("Can not use insert_rows_from with insert or default_values")
        self._rows_source_sql = rows_source_sql
        return self

    @BuilderMethods.builder
    def distinct(self) -> Self:
        self._distinct = True
        return self

    @BuilderMethods.builder
    def distinct_on(self, *fields: str | Term) -> Self:
        for field in fields:
            if isinstance(field, str):
                self._mutable("_distinct_on").append(Field(field))
            elif isinstance(field, Term):
                self._mutable("_distinct_on").append(field)
        return self

    @BuilderMethods.builder
    def for_update(
        self,
        nowait: bool = False,
        skip_locked: bool = False,
        of: Iterable[str] = (),
        strength: str | None = None,
    ) -> Self:
        """Locks the rows read - ``FOR UPDATE`` or a weaker lock.

        Args:
            nowait: Fail at once on a locked row.
            skip_locked: Leave locked rows out.
            of: The tables locked, by alias - every table by default.
            strength: A ``RowLockStrength`` value - None for ``FOR UPDATE``.
        """
        if nowait and skip_locked:
            raise QueryException("for_update() options nowait and skip_locked are mutually exclusive")
        self._for_update = True
        self._for_update_skip_locked = skip_locked
        self._for_update_nowait = nowait
        self._for_update_of = set(of)
        self._for_update_strength = strength
        return self

    @BuilderMethods.builder
    def sample(self, method: str, percent: float, seed: int | None = None) -> Self:
        """Reads a sample of the first FROM table - ``TABLESAMPLE <method> (<percent>) [REPEATABLE
        (<seed>)]``.

        Args:
            method: How the rows are picked.
            percent: The chance of each row or block, in percent.
            seed: The seed repeating the same sample; None for a new one each run.
        """
        self._table_sample = (method, percent, seed)
        return self

    @BuilderMethods.builder
    def do_nothing(self) -> Self:
        if len(self._on_conflict_do_updates) > 0:
            raise QueryException("Can not have two conflict handlers")
        self._on_conflict_do_nothing = True
        return self

    @BuilderMethods.builder
    def returning(self, *terms: Any) -> Self:
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
                self._return_other(self.wrap_constant(term, self._wrapper_class))
        return self

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
                        [join.criterion.tables_ for join in self._joins]  # type:ignore[attr-defined]
                    )
                )
                join_and_base_tables: set[Any] = set(self._from) | join_tables
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

        table: Selectable | None
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

    @BuilderMethods.builder
    def where(self, criterion: Term | EmptyCriterion) -> Self:
        if isinstance(criterion, EmptyCriterion):
            return self
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
        return self

    @BuilderMethods.builder
    def having(self, criterion: Criterion) -> Self:
        if self._havings:
            self._havings &= criterion
        else:
            self._havings = criterion
        return self

    @BuilderMethods.builder
    def groupby(self, *terms: str | int | Term) -> Self:
        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._from[0])
            elif isinstance(term, int):
                field = Field(str(term), table=self._from[0])
                term = field.wrap_constant(term)

            self._mutable("_groupbys").append(term)
        return self

    @BuilderMethods.builder
    def orderby(self, *fields: Any, **kwargs: Any) -> Self:
        for field in fields:
            field = Field(field, table=self._from[0]) if isinstance(field, str) else self.wrap_constant(field)

            self._mutable("_orderbys").append((field, kwargs.get("order")))
        return self

    @BuilderMethods.builder
    def join(
        self,
        item: Table | QueryBuilder | AliasedQuery | Selectable,
        how: JoinType = JoinType.INNER,
    ) -> Joiner:
        if isinstance(item, Table):
            return Joiner(self, item, how, type_label="table")

        if isinstance(item, QueryBuilder):
            if item.alias is None:
                self._tag_subquery(item)
            return Joiner(self, item, how, type_label="subquery")

        if isinstance(item, AliasedQuery):
            return Joiner(self, item, how, type_label="table")

        if isinstance(item, Selectable):
            return Joiner(self, item, how, type_label="subquery")

        raise JoinException(f"Cannot join on type '{type(item)}'")

    @BuilderMethods.builder
    def limit(self, limit: int) -> Self:
        self._limit = cast("ValueWrapper", self.wrap_constant(limit))
        return self

    @BuilderMethods.builder
    def offset(self, offset: int) -> Self:
        self._offset = cast("ValueWrapper", self.wrap_constant(offset))
        return self

    @BuilderMethods.builder
    def union(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.UNION, wrapper_class=self._wrapper_class)

    @BuilderMethods.builder
    def union_all(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.UNION_ALL, wrapper_class=self._wrapper_class)

    @BuilderMethods.builder
    def intersect(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.INTERSECT, wrapper_class=self._wrapper_class)

    @BuilderMethods.builder
    def except_of(self, other: Self) -> SetOperationQuery:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        return SetOperationQuery(self, other, SetOperation.EXCEPT_OF, wrapper_class=self._wrapper_class)

    @BuilderMethods.builder
    def set(self, field: Field | str, value: Any) -> Self:
        field = Field(field) if not isinstance(field, Field) else field
        value = self.wrap_constant(value, wrapper_class=self._wrapper_class)
        self._mutable("_updates").append((field, value))
        return self

    def __add__(self, other: Self) -> SetOperationQuery:  # type:ignore[override]
        return self.union(other)

    def __mul__(self, other: Self) -> SetOperationQuery:  # type:ignore[override]
        return self.union_all(other)

    @BuilderMethods.builder
    def slice(self, slice: slice) -> Self:
        if slice.start is not None:
            self._offset = cast("ValueWrapper", self.wrap_constant(slice.start))
        if slice.stop is not None:
            self._limit = cast("ValueWrapper", self.wrap_constant(slice.stop))
        return self

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
        return self.get_sql(self.query_class.SQL_CONTEXT)

    def __repr__(self) -> str:
        return self.__str__()

    def __eq__(self, other: Any) -> bool:  # type:ignore[override]
        return isinstance(other, QueryBuilder) and self.alias == other.alias

    def __ne__(self, other: Any) -> bool:  # type:ignore[override]
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.alias) + sum(hash(clause) for clause in self._from)

    def _sql_context_with_namespace(self, sql_context: SqlContext) -> SqlContext:
        has_joins = bool(self._joins)
        has_multiple_from_clauses = len(self._from) > 1
        has_subquery_from_clause = len(self._from) > 0 and isinstance(self._from[0], QueryBuilder)
        has_reference_to_foreign_table = self._foreign_table
        has_update_from = self._update_table and self._from

        return sql_context.copy(
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

    def get_sql(self, sql_context: SqlContext | None = None) -> str:
        if not sql_context:
            sql_context = self.query_class.SQL_CONTEXT
        if not self._is_renderable():
            return ""
        sql_context = sql_context.dialect.clauses.get_statement_context(
            self, self._sql_context_with_namespace(sql_context)
        )
        features = sql_context.dialect.features
        if not sql_context.subquery and (
            not features.supports_correlated_subqueries or not features.supports_ordered_correlated_subqueries
        ):
            # Local import: the check reads queries built by this module.
            from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries

            CorrelatedSubqueries.check_statement(self, sql_context)
        if (
            (self._orderbys or self._wheres is not None)
            and not features.orders_by_correlated_subqueries
            and features.supports_correlated_subqueries
            and not self._update_table
            and not self._delete_from
            and not self._insert_table
        ):
            # Local import: the module reads queries built by this one.
            from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries

            if CorrelatedSubqueries.needs_derived_table(self):
                return CorrelatedSubqueries.get_derived_table_sql(self, sql_context)

        if self._update_table:
            if not sql_context.dialect.features.supports_row_updates:
                raise UnSupportedError(
                    f"The {sql_context.dialect.name} database doesn't update stored rows - no UPDATE runs there"
                )
            querystring = sql_context.dialect.clauses.get_update_sql(self, sql_context)
        elif self._delete_from:
            if not sql_context.dialect.features.supports_row_deletes:
                raise UnSupportedError(
                    f"The {sql_context.dialect.name} database doesn't delete stored rows - no DELETE runs there"
                )
            querystring = sql_context.dialect.clauses.get_delete_sql(self, sql_context)
        else:
            querystring = QuerySqlRendering.select_insert_delete_sql(self, sql_context)
        if self._returns:
            querystring += sql_context.dialect.clauses.get_returning_sql(
                QuerySqlRendering.get_returned_values(self, sql_context)
            )
        return querystring

    def _is_renderable(self) -> bool:
        if not (self._selects or self._insert_table or self._delete_from or self._update_table):
            return False
        if self._insert_table and not (
            self._selects or self._values or self._default_values or self._rows_source_sql is not None
        ):
            return False
        return not (self._update_table and not self._updates)

    def get_parameterized_sql(self, sql_context: SqlContext | None = None) -> tuple[str, list[Any]]:
        """
        Returns a tuple containing the query string and a list of parameters
        """
        if not sql_context:
            sql_context = self.query_class.SQL_CONTEXT

        if not sql_context.parameterizer:
            sql_context = sql_context.copy(parameterizer=Parameterizer())

        return (
            self.get_sql(sql_context),
            sql_context.parameterizer.values,  # type: ignore[union-attr]
        )

    def _orderby_sql(
        self,
        sql_context: SqlContext,
        # builtins.set - QueryBuilder.set() shadows the builtin name in this class body.
        selected_aliases: builtins.set[str | None] | None = None,
        grouped_sql_by_term_id: dict[int, str] | None = None,
    ) -> str:
        """Renders the ORDER BY clause. A term selected under an alias is ordered by the alias when
        ``orderby_alias`` is set.

        Args:
            sql_context: The rendering state of the statement.
            selected_aliases: The selected aliases, when the caller already has them.
            grouped_sql_by_term_id: The SQL ``QuerySqlRendering.group_sql()`` rendered - an ORDER BY of the same term
                reuses it, so both carry the same parameters; Postgres matches the two textually.
        """
        clauses = []
        if selected_aliases is None:
            selected_aliases = {select_term.alias for select_term in self._selects}
        # A Subquery/Exists term renders its own parentheses only in subquery context.
        term_context = sql_context.copy(subquery=True)
        grouped_sql_by_term_id = (
            grouped_sql_by_term_id if sql_context.dialect.features.matches_ordering_to_grouping_by_sql else None
        )
        for field, directionality in self._orderbys:
            if sql_context.orderby_alias and field.alias and field.alias in selected_aliases:
                term = sql_context.quote_alias(field.alias)
            elif grouped_sql_by_term_id and field.alias is None and id(field) in grouped_sql_by_term_id:
                term = grouped_sql_by_term_id[id(field)]
            else:
                term = sql_context.dialect.renderers.get_ordering_term(field, sql_context).get_sql(term_context)

            clauses.append(f"{term} {directionality}" if directionality is not None else term)

        return f" ORDER BY {','.join(clauses)}"
