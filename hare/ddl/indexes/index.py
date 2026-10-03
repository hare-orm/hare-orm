from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import Expression, ExpressionContext, F, Ordering
from hare.sql.context import NEUTRAL_SQL_CONTEXT
from hare.sql.enums import Order
from hare.sql.terms.base.term import Term
from hare.sql.terms.field import Field as HareSqlField
from hare.utils.class_path import ClassPath

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.models import Model
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey


class Index:
    """All types of index parent class, default is BTreeIndex.

    Args:
        expressions: The expressions on which the index is desired - an ``F("field").desc(...)``/
            ``.asc(...)`` among them orders that key, NULL placement included.
        fields: A tuple of names of the fields on which the index is desired - ``"-field"`` for a
            descending key.
        name: The name of the index.
        unique: Renders this as a ``CREATE UNIQUE INDEX`` instead of a plain one - the way to get
            a NULL-safe or expression-based uniqueness constraint (e.g. one component wrapped in
            ``Coalesce(...)`` so ``NULL`` participates in the uniqueness check instead of being
            exempt from it the way a plain ``UniqueConstraint``/``unique=True`` field always
            treats it), which a table-level ``UniqueConstraint`` can't express since it only
            takes plain field names. Only a plain btree index (``INDEX_TYPE`` unset) supports
            this - Postgres itself rejects ``UNIQUE`` on every other access method.
        include: Fields stored in the index as non-key columns (Postgres ``INCLUDE``), so a query
            reading only them and the key columns is answered from the index alone. SQLite has no
            such columns - it creates the index without them.

    Raises:
        ConfigurationError: If params conflict.
    """

    INDEX_TYPE = ""
    #: Integer ``WITH (...)`` storage parameters this index class takes as constructor kwargs.
    INTEGER_STORAGE_PARAMETERS: tuple[str, ...] = ()
    #: Whether the access method stores ``INCLUDE`` columns - btree, GiST and SP-GiST do.
    SUPPORTS_INCLUDE: ClassVar[bool] = True

    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if expressions and not fields and all(self.is_field_key(expression) for expression in expressions):
            # F("a")/F("a").desc() keys are the fields ["a"]/["-a"] - one index, one declaration.
            fields = [self.get_field_key_name(cast("F | Ordering", expression)) for expression in expressions]
            expressions = ()
        self.fields, self.field_orders = self.get_fields_and_orders(fields or ())
        if not expressions and not fields:
            raise ConfigurationError("At least one field or expression is required to define an index.")
        if expressions and fields:
            raise ConfigurationError(
                "Index.fields and expressions are mutually exclusive.",
            )
        if opclasses:
            if not fields:
                raise ConfigurationError("Index.opclasses requires Index.fields to be set.")
            if len(opclasses) != len(self.fields):
                raise ConfigurationError("Index.opclasses must have the same length as Index.fields.")
        if unique and self.INDEX_TYPE:
            raise UnSupportedError(
                f"unique=True is not supported together with a {self.INDEX_TYPE} index - "
                "Postgres only supports UNIQUE on a plain btree index."
            )
        self.name = name
        #: The key expressions as declared - what a migration file keeps, and what each database
        #: renders its own SQL for.
        self.declared_expressions: tuple[Any, ...] = tuple(expressions)
        #: The key expressions resolved against the model in plain SQL once ``get_expressions()``
        #: ran - what the index's generated name and its comparisons use, the same on every
        #: database; the declared ones before that.
        self.expressions = expressions
        self._expressions_compiled = False
        self.extra = ""
        self.opclasses = list(opclasses or [])
        self.unique = unique
        if include and not self.SUPPORTS_INCLUDE:
            raise UnSupportedError(f"include= is not supported by a {self.INDEX_TYPE} index.")
        self.include = list(include or [])

    @staticmethod
    def is_field_key(expression: Any) -> bool:
        """Whether an index key is a plain field reference with no NULL placement of its own."""
        if isinstance(expression, Ordering):
            return "__" not in expression.field_name and expression.order.nulls_first is None
        return type(expression) is F and "__" not in expression.name

    @staticmethod
    def get_field_key_name(expression: F | Ordering) -> str:
        """The declared field name of a plain field key - ``"-name"`` when descending."""
        if isinstance(expression, Ordering):
            return expression.field_name if expression.order.is_ascending else f"-{expression.field_name}"
        return expression.name

    def get_key_orders(self) -> list[Order]:
        """Each key's direction and NULL placement, the dialect's defaults (Postgres's: NULLs last
        ascending, first descending) spelled out - equal for two declarations of one index.

        Returns:
            One order per key.
        """
        if self.fields:
            return [Order.DESC_NULLS_FIRST if order else Order.ASC_NULLS_LAST for order in self.field_orders]
        orders: list[Order] = []
        for expression in self.expressions:
            order = expression.order if isinstance(expression, (Ordering, OrderedIndexKey)) else Order.ASC
            nulls_first = order.nulls_first
            if nulls_first is None:
                nulls_first = not order.is_ascending
            orders.append(Order.build(order.is_ascending, nulls_first))
        return orders

    @staticmethod
    def get_fields_and_orders(fields: Iterable[str]) -> tuple[list[str], list[str]]:
        """Splits declared field names into the names and their key orders.

        Args:
            fields: Field names, ``"-name"`` for a descending key.

        Returns:
            The names, and ``"DESC"`` or ``""`` for each.
        """
        names: list[str] = []
        orders: list[str] = []
        for field in fields:
            is_descending = field.startswith("-")
            names.append(field[1:] if is_descending else field)
            orders.append(Order.DESC.value if is_descending else "")
        return names, orders

    def get_declared_fields(self, names: Iterable[str] | None = None) -> list[str]:
        """The field names as declared - ``"-name"`` for a descending key.

        Args:
            names: The names to prefix, in key order - the index's own when None.

        Returns:
            The declared names.
        """
        return [
            f"-{name}" if order else name
            for name, order in zip(self.fields if names is None else names, self.field_orders, strict=True)
        ]

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects a key order the dialect's indexes can't hold.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: A key sets its NULL placement on a dialect whose indexes don't.
        """
        if dialect.supports_index_nulls_order:
            return
        for expression in self.expressions:
            if isinstance(expression, (Ordering, OrderedIndexKey)) and expression.order.nulls_first is not None:
                raise UnSupportedError(
                    f"An index key's NULL placement (nulls_first=/nulls_last=) is not supported on {dialect} - "
                    "its indexes sort NULLs in a fixed place."
                )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        args = list(self.declared_expressions)
        kwargs: dict[str, Any] = {}
        if self.fields:
            kwargs["fields"] = self.get_declared_fields()
        if self.name:
            kwargs["name"] = self.name
        if self.opclasses:
            kwargs["opclasses"] = list(self.opclasses)
        if self.unique:
            kwargs["unique"] = self.unique
        if self.include:
            kwargs["include"] = list(self.include)
        return path, args, kwargs

    def _get_column_names(self, schema_editor: BaseSchemaEditor, model: type[Model]) -> list[str]:
        """The column names of ``field_names`` - each field's source_field. An expression index's raw
        SQL passes through.
        """
        if not self.fields:
            return self.field_names
        return model._meta.get_column_names(self.field_names)

    def get_name_parts(self) -> tuple[str, ...]:
        """What sets this index apart from another unnamed one on the same columns - hashed into
        its generated name. Empty for a plain btree index, whose name depends on its table and
        columns only.

        Returns:
            The access method, operator classes and ``WITH``/``WHERE`` clause, each when set.
        """
        name_parts = [self.INDEX_TYPE] if self.INDEX_TYPE else []
        if any(self.field_orders):
            name_parts.extend(("desc", *(name for name, order in zip(self.fields, self.field_orders) if order)))
        name_parts.extend(self.opclasses)
        if self.include:
            name_parts.extend(("include", *self.include))
        if self.extra:
            name_parts.append(self.extra.strip())
        return tuple(name_parts)

    def get_generated_expression_name(self, table_name: str) -> str:
        """The name an unnamed expression-based index gets on ``table_name``.

        Args:
            table_name: The indexed table.

        Returns:
            The generated index name.
        """
        prefix = GeneratedNamePrefix.UNIQUE_INDEX if self.unique else GeneratedNamePrefix.INDEX
        return GeneratedNames.get_index_name(prefix, table_name, self.field_names, self.get_name_parts())

    def index_name(self, schema_editor: BaseSchemaEditor, model: type[Model]) -> str:
        self.get_expressions(model)
        column_names = self._get_column_names(schema_editor, model)
        prefix = GeneratedNamePrefix.UNIQUE_INDEX if self.unique else GeneratedNamePrefix.INDEX
        return self.name or GeneratedNames.get_index_name(prefix, model, column_names, self.get_name_parts())

    def get_extra(self, model: type[Model], client: DatabaseClient) -> str:
        """The ``INCLUDE``/``WITH``/``WHERE`` clauses following the indexed columns, for a table of
        ``model``.

        Args:
            model: The indexed model.
            client: The client of the database the DDL runs on.

        Returns:
            The clauses, or ``""``.
        """
        self.raise_if_unsupported(client.dialect)
        return self.get_include_sql(model, client) + self.extra

    def get_include_sql(self, model: type[Model], client: DatabaseClient) -> str:
        """The clause of the non-key columns - none where the dialect has no non-key index
        columns, as they only make the index cover more queries.

        Args:
            model: The indexed model.
            client: The client of the database the DDL runs on.

        Returns:
            The clause, or ``""``.
        """
        if not self.include:
            return ""
        columns = model._meta.get_column_names(self.include)
        return client.dialect.get_index_include_sql([client.dialect.quote_identifier(column) for column in columns])

    def get_sql(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> str:
        """Returns the statement creating the index on a table of ``model``.

        Args:
            schema_editor: The schema editor of the database the DDL runs on.
            model: The indexed model.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        self.get_expressions(model)
        return schema_editor._get_index_sql(
            model,
            self.get_key_sqls(model, schema_editor.client.dialect),
            safe,
            index_name=self.index_name(schema_editor, model),
            index_type=self.INDEX_TYPE,
            extra=self.get_extra(model, schema_editor.client),
            opclasses=self.opclasses or None,
            unique=self.unique,
            orders=self.field_orders or None,
        )

    def get_expressions(self, model: type[Model]) -> None:
        """Resolves the declared key expressions against the model in plain SQL, into
        ``expressions`` - once.

        Args:
            model: The indexed model.
        """
        if self._expressions_compiled or not self.expressions:
            return
        if not any(isinstance(expression, (Expression, Ordering)) for expression in self.declared_expressions):
            self._expressions_compiled = True
            return
        self.expressions = tuple(self.get_key_terms(model, SQL_DIALECT))
        self._expressions_compiled = True

    def get_key_sqls(self, model: type[Model], dialect: Dialect) -> list[str]:
        """Returns the index's keys as a dialect's ``CREATE INDEX`` lists them: the column of a
        field key, the SQL the dialect renders for an expression key - so an index declared once
        is created with each database's own SQL.

        Args:
            model: The indexed model.
            dialect: The dialect of the database the DDL runs on.

        Returns:
            One key each - a column name, ``(expression)``, or ``(expression) DESC ...``.
        """
        if self.fields:
            return model._meta.get_column_names(self.fields)
        ctx = dialect.sql_context.copy(native_functions_only=True)
        return [
            term.get_sql(ctx) if isinstance(term, OrderedIndexKey) else f"({term.get_sql(ctx)})"
            for term in self.get_key_terms(model, dialect)
        ]

    def get_key_terms(self, model: type[Model], dialect: Dialect) -> list[Term]:
        """Resolves the declared key expressions against the model for a dialect.

        Args:
            model: The indexed model.
            dialect: The dialect the terms render for.

        Returns:
            One term each - a declared term (``RawSQLTerm``) as it is.
        """
        expression_context = ExpressionContext(
            model=model,
            table=model.get_table(),
            annotations={},
            dialect=dialect,
            connection=None,
        )
        key_terms: list[Term] = []
        for expression in self.declared_expressions:
            if isinstance(expression, Ordering):
                column = self.get_key_column(model, expression_context, expression.field_name)
                key_terms.append(OrderedIndexKey(column, expression.order))
            elif type(expression) is F and expression.name in model._meta.fields_db_projection:
                key_terms.append(self.get_key_column(model, expression_context, expression.name))
            elif isinstance(expression, Expression):
                result = expression.get_result(expression_context)
                key_terms.append(result.term)
            else:
                key_terms.append(expression)
        return key_terms

    @staticmethod
    def get_key_column(model: type[Model], expression_context: ExpressionContext, field_name: str) -> HareSqlField:
        """The column of a field an index key names.

        Args:
            model: The indexed model.
            expression_context: The context the index's expressions resolve in.
            field_name: The field.

        Returns:
            The column term.

        Raises:
            ConfigurationError: The name isn't a field of the model.
        """
        column_name = model._meta.fields_db_projection.get(field_name)
        if column_name is None:
            raise ConfigurationError(f"Index key {field_name!r} is not a field of {model.__name__}")
        # Unqualified, as an introspected index's key is - the two compare by their SQL text.
        return HareSqlField(column_name)

    @property
    def field_names(self) -> list[str]:
        if self.fields:
            return list(self.fields)
        elif self.expressions:
            if any(isinstance(expression, (Expression, Ordering)) for expression in self.expressions):
                raise ConfigurationError("Index expressions must be resolved before accessing field_names.")
            return [
                expression.get_sql(NEUTRAL_SQL_CONTEXT)
                if isinstance(expression, OrderedIndexKey)
                else f"({cast('Term', expression).get_sql(NEUTRAL_SQL_CONTEXT)})"
                for expression in self.expressions
            ]
        else:
            raise ConfigurationError("At least one field or expression is required to define an index.")

    def __repr__(self) -> str:
        argument = ""
        if self.expressions:
            argument += ", ".join(map(str, self.expressions))
        if self.fields:
            fields = self.get_declared_fields()
            argument += f"{fields=}"
        if name := self.name:
            argument += f", {name=}"
        if opclasses := self.opclasses:
            argument += f", {opclasses=}"
        if self.unique:
            argument += ", unique=True"
        if include := self.include:
            argument += f", {include=}"
        return self.__class__.__name__ + "(" + argument + ")"

    def __hash__(self) -> int:
        # Changes once get_expressions() resolves the expressions - nothing puts an unresolved Index
        # into a set or dict.
        return hash(
            (
                tuple(self.fields),
                tuple(self.field_orders),
                self.name,
                tuple(self.expressions),
                tuple(self.opclasses),
                self.unique,
                tuple(self.include),
            )
        )

    def __eq__(self, other: Any) -> bool:
        return type(self) is type(other) and self.__dict__ == other.__dict__
