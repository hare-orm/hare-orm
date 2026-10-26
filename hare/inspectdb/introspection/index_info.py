from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.inspectdb.constants import DEFAULT_KEY_ORDERS
from hare.query.expressions import F, Ordering
from hare.sql.order import Order
from hare.sql.terms.field import Field as HareSqlField
from hare.sql.terms.term import Term


@dataclass
class IndexInfo:
    """A multi-column index or constraint, or a single-column one with what a field flag can't express
    (an opclass, another method, a condition) - rebuilt as a UniqueConstraint or a Meta.indexes
    entry. A plain single-column one is on ColumnInfo.
    """

    columns: list[str]
    is_unique: bool
    #: The index's own name, as declared in the database - preserved so a reconstructed Index
    #: can be matched/removed by name (RemoveIndex requires a name or field list; an unnamed,
    #: expression-based reconstruction has neither) instead of only ever comparing by shape.
    name: str = ""
    #: The index's access method, lowercase ("gin", "gist", "hash", "brin", "spgist", "bloom") - "" for
    #: the default one.
    index_type: str = ""
    #: Every column's opclass, set once any is non-default - Index.opclasses takes one per field.
    opclasses: list[str] = dataclass_field(default_factory=list)
    #: Every column's opclass name when all of them are the access method's defaults (and
    #: opclasses is therefore left empty) - where the database has opclasses.
    default_opclasses: list[str] = dataclass_field(default_factory=list)
    #: Each key term's SQL, in order, for an index with an expression term - rebuilt as
    #: Index(*expressions). None for plain columns.
    expression_terms: list[str] | None = None
    #: The index's ``WITH (...)`` storage parameters, e.g.
    #: ``{"lists": "5"}`` - raw value text, empty when none were set.
    storage_parameters: dict[str, str] = dataclass_field(default_factory=dict)
    #: The UNIQUE constraint backed by this index is DEFERRABLE.
    deferrable: bool = False
    #: The UNIQUE constraint backed by this index is INITIALLY DEFERRED.
    initially_deferred: bool = False
    #: The partial-index predicate as the database reports it, one outer pair of parentheses
    #: removed - None for an index that isn't partial.
    condition_sql: str | None = None
    #: The index's non-key (``INCLUDE``) columns - None where the database has no such columns, so a
    #: declared ``include`` can't be checked there.
    include: list[str] | None = None
    #: The unique index treats NULLs as equal (``NULLS NOT DISTINCT``).
    nulls_not_distinct: bool = False
    #: The unique constraint backed by this index holds its last key ``WITHOUT OVERLAPS`` - the index's
    #: access method and opclasses are the constraint's own, not declared.
    without_overlaps: bool = False
    #: Whether the index is complete - False for one whose build didn't finish (a failed concurrent
    #: build), which the database keeps but doesn't use.
    is_valid: bool = True
    #: Each key's direction and NULL placement (an ``Order`` value, the database's defaults spelled
    #: out) - an expression key's too; empty for
    #: ascending keys whose order wasn't read.
    key_orders: list[str] = dataclass_field(default_factory=list)
    #: What the index has that no hare index or constraint can declare - the sort order of a
    #: collated key - each described as SQL text (``a COLLATE NOCASE DESC``);
    #: reconstructed without it and flagged in a comment.
    unrepresentable_properties: list[str] = dataclass_field(default_factory=list)

    def is_special(self, *, unique_include_is_constraint: bool = False) -> bool:
        """Whether the index has what a plain ``Index(fields=...)``/``UniqueConstraint`` can't
        express - an access method, an opclass, a condition, an expression key, a NULL placement
        or non-key columns.

        Args:
            unique_include_is_constraint: The non-key columns of a unique index don't count -
                they become its ``UniqueConstraint``'s ``include``.

        Returns:
            True when it is declared through ``get_index_declaration()``.
        """
        return bool(
            self.index_type
            or self.opclasses
            or self.condition_sql
            or self.expression_terms
            or (self.include and not (unique_include_is_constraint and self.is_unique))
            or self.has_explicit_null_placement()
        )

    def get_index_declaration(
        self,
        index_classes_by_type: Mapping[str, type[Index]],
        field_names_by_column: Mapping[str, str],
        *,
        as_key_terms: bool,
    ) -> tuple[type[Index], list[Any], dict[str, Any]]:
        """The ``Index`` declaring a special index, without its name.

        ``fields=`` and expressions are mutually exclusive, so an index with an expression key
        gets every key - plain columns too - as a ``RawSQLTerm``. ``unique=True`` is kept only
        for the default access method: the others can't be unique, and no index class of theirs
        takes it.

        Args:
            index_classes_by_type: The dialect's index class of each access method.
            field_names_by_column: The field name of each column - a column without one keeps
                its own name.
            as_key_terms: Keys with a NULL placement as an index resolves them (terms over
                columns), not as a model declares them (``F``/``Ordering`` over fields).

        Returns:
            The index class, its positional arguments and its keyword arguments.
        """
        arguments: list[Any] = []
        keyword_arguments: dict[str, Any] = {}
        field_names = self.get_key_field_names(field_names_by_column)
        if self.expression_terms is not None:
            key_orders = self.key_orders or [Order.ASC_NULLS_LAST.value] * len(self.expression_terms)
            arguments = [
                RawSQLTerm(term)
                if key_order == Order.ASC_NULLS_LAST
                else OrderedIndexKey(RawSQLTerm(term), Order(key_order))
                for term, key_order in zip(self.expression_terms, key_orders, strict=True)
            ]
        elif self.has_explicit_null_placement():
            arguments = (
                self.get_ordered_key_terms(list(self.columns)) if as_key_terms else self.get_ordered_keys(field_names)
            )
        else:
            keyword_arguments["fields"] = self.get_declared_relation_fields(field_names_by_column)
            if self.opclasses:
                keyword_arguments["opclasses"] = list(self.opclasses)
        if self.include:
            keyword_arguments["include"] = [field_names_by_column.get(column, column) for column in self.include]
        if self.condition_sql is not None:
            keyword_arguments["condition"] = RawSQLTerm(self.condition_sql)
        # Only the default access method's class comes without a condition - the dialect's own
        # classes all take one.
        index_class = index_classes_by_type.get(
            self.index_type, PartialIndex if self.condition_sql is not None else Index
        )
        if self.is_unique and self.index_type not in index_classes_by_type:
            keyword_arguments["unique"] = True
        for parameter_name in index_class.INTEGER_STORAGE_PARAMETERS:
            if parameter_name in self.storage_parameters:
                keyword_arguments[parameter_name] = int(self.storage_parameters[parameter_name])
        for parameter_name in index_class.TEXT_STORAGE_PARAMETERS:
            if parameter_name in self.storage_parameters:
                keyword_arguments[parameter_name] = self.storage_parameters[parameter_name]
        for parameter_name in index_class.FLOAT_STORAGE_PARAMETERS:
            if parameter_name in self.storage_parameters:
                keyword_arguments[parameter_name] = float(self.storage_parameters[parameter_name])
        return index_class, arguments, keyword_arguments

    def has_explicit_null_placement(self) -> bool:
        """Whether a key places NULLs other than its direction's default - declared as
        ``F(...).asc(nulls_first=True)``/``F(...).desc(nulls_last=True)``."""
        return any(key_order not in DEFAULT_KEY_ORDERS for key_order in self.key_orders)

    def get_declared_fields(self, field_names: list[str]) -> list[str]:
        """The keys' field names as ``Index(fields=...)`` declares them - ``"-name"`` descending.

        Args:
            field_names: Each key's field name.

        Returns:
            The declared names.
        """
        return [
            f"-{field_name}" if key_order.startswith(Order.DESC.value) else field_name
            for field_name, key_order in zip(field_names, self.key_orders or [""] * len(field_names), strict=True)
        ]

    def has_descending_key(self) -> bool:
        """Whether a key of the index is descending.

        Returns:
            True when one is.
        """
        return any(key_order.startswith(Order.DESC.value) for key_order in self.key_orders or ())

    def get_key_field_names(self, field_names_by_column: Mapping[str, str]) -> list[str]:
        """The field name of each key column - a column of a relation of several key columns named
        by itself, a key of its own.

        Args:
            field_names_by_column: The field name of each column.

        Returns:
            The names, one per column.
        """
        relation_column_counts = Counter(field_names_by_column.values())
        return [
            column
            if relation_column_counts[field_names_by_column.get(column, column)] > 1
            else field_names_by_column.get(column, column)
            for column in self.columns
        ]

    def get_declared_relation_fields(self, field_names_by_column: Mapping[str, str]) -> list[str]:
        """The keys' field names as ``Index(fields=...)`` declares them - ``"-name"`` descending. A
        relation of several key columns is named once where its columns follow each other in the
        order of its key, in one direction; anywhere else each of them is named by its own column.

        Args:
            field_names_by_column: The field name of each column, a relation's columns in the order
                of its key - a column without one keeps its own name.

        Returns:
            The declared names.
        """
        columns_by_field_name: dict[str, list[str]] = {}
        for mapped_column, mapped_field_name in field_names_by_column.items():
            columns_by_field_name.setdefault(mapped_field_name, []).append(mapped_column)
        columns = self.columns
        key_orders = self.key_orders or [""] * len(columns)
        declared_fields: list[str] = []
        position = 0
        while position < len(columns):
            column = columns[position]
            key_order = key_orders[position]
            field_name = field_names_by_column.get(column, column)
            relation_columns = columns_by_field_name.get(field_name, [column])
            width = len(relation_columns)
            if width > 1 and (
                columns[position : position + width] != relation_columns
                or len(set(key_orders[position : position + width])) != 1
            ):
                # Not the relation's whole key in its order - the column itself.
                field_name = column
                width = 1
            declared_fields.append(f"-{field_name}" if key_order.startswith(Order.DESC.value) else field_name)
            position += width
        return declared_fields

    def get_ordered_key_terms(self, column_names: list[str]) -> list[Term]:
        """The keys as an index declared with ``get_ordered_keys()`` resolves them.

        Args:
            column_names: Each key's column.

        Returns:
            The column for an ascending key with NULLs last, an ordered key otherwise.
        """
        return [
            HareSqlField(column_name)
            if key_order == Order.ASC_NULLS_LAST
            else OrderedIndexKey(HareSqlField(column_name), Order(key_order))
            for column_name, key_order in zip(column_names, self.key_orders, strict=True)
        ]

    def get_ordered_keys(self, field_names: list[str]) -> list[F | Ordering]:
        """The keys as ``Index(*expressions)`` declares them, each with its order.

        Args:
            field_names: Each key's field name.

        Returns:
            ``F(name)`` for an ascending key with NULLs last, an ordering otherwise.
        """
        return [
            F(field_name) if key_order == Order.ASC_NULLS_LAST else Ordering(field_name, Order(key_order))
            for field_name, key_order in zip(field_names, self.key_orders, strict=True)
        ]
