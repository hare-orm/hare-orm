from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.query.expressions.constants import JSON_TABLE_COLUMN_NAME_PATTERN, JSON_TABLE_DEFAULT_COLUMN_PATH
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.joins.json_table_column import JsonTableColumn
from hare.query.expressions.joins.named_columns import NamedColumns
from hare.sql.builder.tables.json_table_query import JsonTableQuery
from hare.sql.terms.criteria.true_criterion import TrueCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext


class JsonTable(NamedColumns):
    """The items of a JSON document as rows joined under the name ``alias()``/``annotate()`` gives
    it - ``JSON_TABLE``::

        Order.objects.alias(
            line=JsonTable(
                "document", "$.lines[*]", {"sku": fields.CharField(max_length=20), "quantity": fields.IntField()}
            )
        ).filter(line__quantity__gte=10).values("id", "line__sku")

    ``<name>__<column>`` reads a column, in filters, expressions, ``values()`` and ``order_by()``. A row
    comes once for each item; a row whose document has none still comes, its columns NULL - a LEFT
    JOIN. PostgreSQL 17+: another database raises ``UnSupportedError`` before the query is sent.

    Args:
        document: The JSON field - a name, ``F()`` or an expression.
        path: The JSON path of the items (``$.lines[*]``).
        columns: The columns by name - each the field of its type, read at ``$.<name>`` of the item,
            or a ``(field, path)`` pair reading another path.
        ordinality: A column numbering the items from 1, None for none.

    Raises:
        QueryError: An argument is of the wrong type - a path not starting with ``$``, a column name
            that isn't an identifier, a column that isn't a field.
    """

    #: The field of the ordinality column.
    ORDINALITY_FIELD: ClassVar[IntField[Any]] = IntField()

    def __init__(
        self,
        document: str | Expression,
        path: str,
        columns: Mapping[str, Field[Any] | tuple[Field[Any], str]],
        *,
        ordinality: str | None = None,
    ) -> None:
        if not isinstance(path, str) or not path.startswith("$"):
            raise QueryError(f"JsonTable() takes a JSON path starting with $, got {path!r}")
        if not isinstance(columns, Mapping) or not columns:
            raise QueryError("JsonTable() takes its columns as a non-empty dict of name -> field")
        self.document = F(document) if isinstance(document, str) else document
        self.path = path
        self.columns = tuple(self.get_column(name, column) for name, column in columns.items())
        if ordinality is not None:
            self.check_column_name(ordinality)
            if ordinality in columns:
                raise QueryError(f"JsonTable() ordinality {ordinality!r} is the name of one of its columns")
        self.ordinality = ordinality

    @staticmethod
    def check_column_name(name: Any) -> None:
        """Rejects a column name that isn't a Python identifier without ``__``.

        Raises:
            QueryError: It is another name.
        """
        if not isinstance(name, str) or JSON_TABLE_COLUMN_NAME_PATTERN.fullmatch(name) is None:
            raise QueryError(f"A JsonTable() column name is an identifier without '__', got {name!r}")

    @classmethod
    def get_column(cls, name: str, column: Any) -> JsonTableColumn:
        """One column as declared - a field, or a ``(field, path)`` pair.

        Raises:
            QueryError: The name or the declaration is wrong.
        """
        cls.check_column_name(name)
        field, path = (
            column if isinstance(column, tuple) else (column, JSON_TABLE_DEFAULT_COLUMN_PATH.format(name=name))
        )
        if not isinstance(field, Field) or not isinstance(path, str) or not path.startswith("$"):
            raise QueryError(
                f"A JsonTable() column is a field or a (field, path) pair with a path starting with $, got {column!r}"
            )
        return JsonTableColumn(name, field, path)

    def get_column_field(self, column_name: str) -> Field[Any]:
        """The field a column is read through.

        Raises:
            FieldError: It isn't a column of the table.
        """
        if column_name == self.ordinality:
            return self.ORDINALITY_FIELD  # type: ignore[call-overload]
        for column in self.columns:
            if column.name == column_name:
                return column.field
        names = [column.name for column in self.columns] + ([self.ordinality] if self.ordinality else [])
        raise FieldError(f"JsonTable {self.name!r} reads one of its columns ({', '.join(names)}), got {column_name!r}")

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """The column ``path`` names, read from the table joined under the name.

        Raises:
            QueryError: It is used before ``alias()``/``annotate()`` named it.
            UnSupportedError: The database has no ``JSON_TABLE``.
            FieldError: ``path`` isn't one of its columns.
        """
        if self.name is None:
            raise QueryError("A JsonTable is used through the name alias()/annotate() gives it")
        # A context of no connection only probes which names a query reads - the query run checks.
        connection = expression_context.connection
        if connection is not None and not connection.features.supports_json_table:
            raise UnSupportedError(
                f"JsonTable {self.name!r} needs JSON_TABLE, which the {expression_context.dialect} server of this "
                "connection doesn't have"
            )
        column_field = self.get_column_field(path)
        document_result = self.document.get_result(expression_context)
        json_table_query = JsonTableQuery(self.name, document_result.term, self.path, self.columns, self.ordinality)
        return ExpressionResult(
            term=json_table_query.field(path),
            joins=[*document_result.joins, (json_table_query, TrueCriterion())],  # type: ignore[list-item]
            output_field=column_field,
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # The name read alone (an alias a path uses) is its JOIN - values() refuses to select it.
        return self.get_path_result(self.columns[0].name, expression_context)

    def __repr__(self) -> str:
        return f"JsonTable({self.document!r}, {self.path!r})"
