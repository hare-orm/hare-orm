from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.classes.class_path import ClassPath
from hare.ddl.declared_names import DeclaredNames
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.expressions.declared_expressions import DeclaredExpressions
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import Expression, F, Ordering
from hare.sql.constants import INDEX_KEY_SQL_CONTEXT
from hare.sql.order import Order
from hare.sql.terms.field import Field as HareSqlField
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model
from hare.ddl.ddl_term_sql import DdlTermSql
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey


class Index:
    """An index of a model - the database's default type of index, or the base of an index of another
    type (``INDEX_TYPE``).

    Args:
        expressions: The expressions on which the index is desired - an ``F("field").desc(...)``/
            ``.asc(...)`` among them orders that key, NULL placement included.
        fields: A tuple of names of the fields on which the index is desired - ``"-field"`` for a
            descending key.
        name: The name of the index.
        opclasses: The operator class of each key field, where the dialect has them, one per name in
            ``fields`` - e.g.
            ``"text_pattern_ops"``.
        unique: Renders this as a ``CREATE UNIQUE INDEX`` instead of a plain one - the way to get
            a NULL-safe or expression-based uniqueness constraint (e.g. one component wrapped in
            ``Coalesce(...)`` so ``NULL`` participates in the uniqueness check instead of being
            exempt from it the way a plain ``UniqueConstraint``/``unique=True`` field always
            treats it), which a table-level ``UniqueConstraint`` can't express since it only
            takes plain field names. Only a plain btree index (``INDEX_TYPE`` unset) supports
            this - a database refuses ``UNIQUE`` on another access method.
        include: Fields stored in the index as non-key columns (``INCLUDE``), so a query reading only
            them and the key columns is answered from the index alone. A dialect without such columns
            creates the index without them.

    Raises:
        ConfigurationError: If params conflict.
    """

    INDEX_TYPE = ""
    #: Integer ``WITH (...)`` storage parameters this index class takes as constructor kwargs.
    INTEGER_STORAGE_PARAMETERS: tuple[str, ...] = ()
    #: Text options this index class takes as constructor kwargs - read back from what the
    #: dialect's introspector reports as the index's storage parameters.
    TEXT_STORAGE_PARAMETERS: tuple[str, ...] = ()
    #: The arguments of the index that are numbers with a fraction - read back from an introspected
    #: index as a float.
    FLOAT_STORAGE_PARAMETERS: tuple[str, ...] = ()
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
        index_class_name = type(self).__name__
        fields = DeclaredNames.get_checked_names(fields, f"{index_class_name}.fields")
        opclasses = DeclaredNames.get_checked_names(opclasses, f"{index_class_name}.opclasses")
        include = DeclaredNames.get_checked_names(include, f"{index_class_name}.include")
        DeclaredNames.raise_if_not_name(name, f"{index_class_name}.name", optional=True)
        for expression in expressions:
            if isinstance(expression, str):
                raise ConfigurationError(
                    f"{index_class_name} takes the key expressions - a field name {expression!r} is "
                    f"F({expression!r}), or fields=[{expression!r}]"
                )
        if expressions and not fields and all(self.is_field_key(expression) for expression in expressions):
            # F("a")/F("a").desc() keys are the fields ["a"]/["-a"] - one index, one declaration.
            fields = [self.get_field_key_name(cast("F | Ordering", expression)) for expression in expressions]
            expressions = ()
        self.fields, self.field_orders = self.get_fields_and_orders(fields or ())
        if len(set(self.fields)) != len(self.fields):
            raise ConfigurationError(f"{index_class_name}.fields names a field twice: {list(self.fields)}")
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
                "only a plain index can be unique."
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
            return (
                isinstance(expression.target, str)
                and "__" not in expression.target
                and expression.order.nulls_first is None
            )
        return type(expression) is F and "__" not in expression.name

    @staticmethod
    def get_field_key_name(expression: F | Ordering) -> str:
        """The declared field name of a plain field key - ``"-name"`` when descending."""
        if isinstance(expression, Ordering):
            field_name = cast("str", expression.target)
            return field_name if expression.order.is_ascending else f"-{field_name}"
        return expression.name

    def get_key_orders(self) -> list[Order]:
        """Each key's direction and NULL placement, the dialect's defaults spelled out - equal for two
        declarations of one index.

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

    def get_required_extension(self, dialect: Dialect, fields_by_name: Mapping[str, Field[Any]]) -> str | None:
        """The database extension the index needs on a dialect - none for a plain index; an index of one
        database's access method names its own.

        Args:
            dialect: The dialect.
            fields_by_name: The fields of the index's model, by name.
        """
        return None

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects a key order the dialect's indexes can't hold.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: A key sets its direction on a dialect whose indexes keep no key order,
                or its NULL placement on one whose indexes don't.
        """
        if not dialect.features.supports_index_key_order and (
            any(self.field_orders)
            or any(isinstance(expression, (Ordering, OrderedIndexKey)) for expression in self.expressions)
        ):
            raise UnSupportedError(
                f'An index key\'s direction ("-field", .asc(), .desc()) is not supported on {dialect} - its '
                "indexes keep no key order."
            )
        if dialect.features.supports_index_nulls_order:
            return
        for expression in self.expressions:
            if isinstance(expression, (Ordering, OrderedIndexKey)) and expression.order.nulls_first is not None:
                raise UnSupportedError(
                    f"An index key's NULL placement (nulls_first=/nulls_last=) is not supported on {dialect} - "
                    "its indexes sort NULLs in a fixed place."
                )

    def raise_if_not_droppable(self, features: Features, dialect: Dialect) -> None:
        """Rejects dropping the index on a database it can't exist on - nothing by default.

        Args:
            features: The features of the connection the DDL runs on.
            dialect: Its dialect.
        """

    def get_stored_form(self) -> Index:
        """The index the database keeps for this declaration - what migrations and drift compare. The
        index itself; an index of a type the database keeps as its plain index gives that.

        Returns:
            The index.
        """
        return self

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The declaration as a migration writes it.

        Returns:
            The import path of its class, its positional arguments and the keyword arguments it was given
            other than by default.
        """
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

    def get_name_parts(self) -> tuple[str, ...]:
        """What sets this index apart from another unnamed one on the same columns - hashed into
        its generated name. Empty for a plain btree index, whose name depends on its table and
        columns only.

        Returns:
            The access method, operator classes and ``WITH``/``WHERE`` clause, each when set.
        """
        name_parts = [self.INDEX_TYPE] if self.INDEX_TYPE else []
        if any(self.field_orders):
            name_parts.extend(
                ("desc", *(name for name, order in zip(self.fields, self.field_orders, strict=True) if order))
            )
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

    def get_index_type_sql(self) -> str:
        """The index's type as its ``CREATE INDEX`` names it - with the arguments of a type taking
        them.

        Returns:
            ``INDEX_TYPE`` by default, ``""`` for the database's default index.
        """
        return self.INDEX_TYPE

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
        literals = client.dialect.literals
        return client.dialect.schema_editor_class.index_statements_class.get_index_include_sql(
            [literals.quote_identifier(column) for column in columns]
        )

    def get_sql(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> str:
        """Returns the statement creating the index on a table of ``model`` - the editor's own, as
        when the index is added later.

        Args:
            schema_editor: The schema editor of the database the DDL runs on.
            model: The indexed model.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        return schema_editor.index_statements.get_index_create_sql(model, self, safe=safe)

    def get_column_orders(self, model: type[Model]) -> list[str]:
        """The direction of each key column - a relation of several key columns takes its field's
        direction on every one of them.

        Args:
            model: The indexed model.

        Returns:
            ``"DESC"`` or ``""`` for each column; none when no key is descending.
        """
        if not any(self.field_orders):
            return []
        meta = model._meta
        return [
            order
            for name, order in zip(self.fields, self.field_orders, strict=True)
            for _column_name in meta.get_column_names((name,))
        ]

    def get_expressions(self, model: type[Model]) -> None:
        """Resolves the declared key expressions against the model in the dialect of
        ``get_expression_dialect()``, into ``expressions`` - once.

        Args:
            model: The indexed model.
        """
        if self._expressions_compiled or not self.expressions:
            return
        if not any(isinstance(expression, (Expression, Ordering)) for expression in self.declared_expressions):
            self._expressions_compiled = True
            return
        self.expressions = tuple(self.get_key_terms(model, self.get_expression_dialect()))
        self._expressions_compiled = True

    def get_expression_dialect(self) -> Dialect:
        """The dialect the key expressions are resolved in once, for the index's name and its
        comparison with other declarations and the database's indexes - plain SQL by default.

        Returns:
            The dialect.
        """
        return SQL_DIALECT

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
        sql_context = dialect.sql_context.copy(native_functions_only=True)
        return [
            DdlTermSql.get_sql(term, sql_context, model, declared)
            if isinstance(term, OrderedIndexKey)
            else f"({DdlTermSql.get_sql(term, sql_context, model, declared)})"
            for term, declared in zip(self.get_key_terms(model, dialect), self.declared_expressions, strict=True)
        ]

    def get_key_terms(self, model: type[Model], dialect: Dialect) -> list[Term]:
        """Resolves the declared key expressions against the model for a dialect.

        Args:
            model: The indexed model.
            dialect: The dialect the terms render for.

        Returns:
            One term each - a declared term (``RawSQLTerm``) as it is.
        """
        key_terms: list[Term] = []
        for expression in self.declared_expressions:
            if isinstance(expression, Ordering) and isinstance(expression.target, str):
                column = self.get_key_column(model, expression.target)
                key_terms.append(OrderedIndexKey(column, expression.order))
            elif isinstance(expression, Ordering):
                term = DeclaredExpressions.get_term(cast("Expression", expression.target), model, dialect)
                key_terms.append(OrderedIndexKey(term, expression.order))
            elif type(expression) is F and expression.name in model._meta.fields_db_projection:
                key_terms.append(self.get_key_column(model, expression.name))
            elif isinstance(expression, Expression):
                key_terms.append(DeclaredExpressions.get_term(expression, model, dialect))
            else:
                key_terms.append(expression)
        return key_terms

    @staticmethod
    def get_key_column(model: type[Model], field_name: str) -> HareSqlField:
        """The column of a field an index key names.

        Args:
            model: The indexed model.
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
    def has_unresolved_expressions(self) -> bool:
        """Whether a key expression still waits to be resolved against the model (``get_expressions()``)."""
        return any(isinstance(expression, (Expression, Ordering)) for expression in self.expressions or ())

    @property
    def field_names(self) -> list[str]:
        """The index's keys as text: its fields' names, or the SQL of each expression.

        Raises:
            ConfigurationError: The expressions aren't built yet, or the index has no key.
        """
        if self.fields:
            return list(self.fields)
        if self.expressions:
            if self.has_unresolved_expressions:
                raise ConfigurationError("Index expressions must be resolved before accessing field_names.")
            return [
                expression.get_sql(INDEX_KEY_SQL_CONTEXT)
                if isinstance(expression, OrderedIndexKey)
                else f"({cast('Term', expression).get_sql(INDEX_KEY_SQL_CONTEXT)})"
                for expression in self.expressions
            ]
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
