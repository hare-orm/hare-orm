from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.fields.relations.relation_values import RelationValues
from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.row_value_list import RowValueList

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.models.meta_info import MetaInfo
    from hare.query.expressions.conditions.q import Q


class KeyColumns:
    """Keys - primary keys and the keys relations point at - over one column or several, and
    the row values comparing them."""

    @staticmethod
    def get_source_columns(meta: MetaInfo) -> tuple[str, ...]:
        """The model's own real DB column(s) backing its primary key, in PK order - a single
        ``db_pk_column`` for a plain PK, one column per component for a composite one."""
        if meta.has_composite_primary_key:
            return tuple(
                field.source_field or name
                for field, name in zip(meta.pk_fields, meta.primary_key_attribute_names, strict=True)
            )
        return (meta.db_pk_column,)

    @classmethod
    def get_row_correlation(cls, meta: MetaInfo, inner_table: Table, outer_table: Table) -> Criterion:
        """Matches a row of ``inner_table`` - a second reference to the model's table - with the
        outer query's row: by primary key, or, for a model without one, by every column, NULLs
        matching NULLs. Rows equal in every column are indistinguishable, so a condition holds
        for one exactly when it holds for the other.

        Args:
            meta: The model's meta.
            inner_table: The subquery's reference to the table.
            outer_table: The outer query's reference to it.

        Returns:
            The correlation criterion.
        """
        if meta.has_primary_key:
            columns = cls.get_source_columns(meta)
            return cls.row_equality(
                [inner_table[column] for column in columns], [outer_table[column] for column in columns]
            )
        correlation: Criterion | None = None
        for column in sorted(meta.db_fields):
            column_matches = inner_table[column].is_distinct_from(outer_table[column]).negate()
            correlation = column_matches if correlation is None else correlation & column_matches
        return cast("Criterion", correlation)

    @staticmethod
    def get_db_values(meta: MetaInfo, instance: Model, types: TypeRegistry) -> tuple[Any, ...]:
        """``instance``'s primary key as bound for its columns, always as a tuple - a single
        element for a plain PK, one per component for a composite one.

        Args:
            meta: The model's meta.
            instance: The instance.
            types: The type registry of the dialect the query runs on.

        Returns:
            The bound values.
        """
        if meta.has_composite_primary_key:
            return tuple(
                types.get_db_value(field, getattr(instance, name), instance)
                for field, name in zip(meta.pk_fields, meta.primary_key_attribute_names, strict=True)
            )
        return (types.get_db_value(meta.pk, instance.pk, instance),)

    @staticmethod
    def row_is_in(columns: Sequence[Term], value_rows: Sequence[Sequence[Any]]) -> Criterion:
        """Builds a column-set membership check: a plain ``.isin(...)`` for a single-column key, a
        ``Tuple(...).isin(RowValueList(Tuple(...), ...))`` row-value membership check for a composite one -
        a dialect writes the list of rows its own way.

        Args:
            columns: The key columns being checked, in a fixed order.
            value_rows: Bound value rows to check membership against, each in the same order as
                ``columns``.
        """
        if len(columns) == 1:
            return columns[0].isin([row[0] for row in value_rows])
        return Tuple(*columns).isin(RowValueList(*[Tuple(*row) for row in value_rows]))

    @staticmethod
    def row_equality(left_columns: Sequence[Term], right_columns: Sequence[Term]) -> Criterion:
        """Builds a column-to-column equality: a plain ``==`` for a single-column key, a
        ``Tuple(...) == Tuple(...)`` row-value comparison for a composite one - the shape a JOIN
        condition needs when both sides are real table columns, not bound values.

        Args:
            left_columns: This side's key columns, in the same order as ``right_columns``.
            right_columns: The other side's key columns, in the same order as ``left_columns``.
        """
        if len(left_columns) == 1:
            return left_columns[0] == right_columns[0]
        return Tuple(*left_columns) == Tuple(*right_columns)

    @staticmethod
    def get_primary_key_q(model: type[Model], key: str, value: Any) -> Q | None:
        """``pk=``/``pk__in=`` of a model with a composite primary key - ``pk`` as comparisons of
        the key's fields, ``pk__in`` checked and kept as one kwarg, which ``Q`` resolves into one
        row-value membership check (or ``(a, b) IN (SELECT ...)`` for a query); ``pk__not=``/
        ``pk__not_in=`` as their negation.

        Args:
            model: The filtered model.
            key: The filter kwarg.
            value: Its value.

        Returns:
            The ``Q``, or None for another key or a model whose primary key is one field.

        Raises:
            QueryError: A key value isn't a tuple of the key's length.
        """
        primary_key_attribute = model._meta.primary_key_attribute
        if not isinstance(primary_key_attribute, tuple) or not primary_key_attribute:
            return None
        # Local imports: the query package imports this module.
        from hare.query.enums import Lookup
        from hare.query.expressions.conditions.q import Q
        from hare.query.expressions.subqueries.declarations import KeyRowsQuery
        from hare.query.expressions.subqueries.subquery import Subquery
        from hare.query.queryset.query_specification import QuerySpecification

        if key in {"pk__not", "pk__not_in"}:
            # The key's columns are never NULL - the negation is the plain NOT of the comparison.
            positive_q = KeyColumns.get_primary_key_q(model, "pk" if key == "pk__not" else "pk__in", value)
            return None if positive_q is None else ~positive_q
        if key == "pk":
            return KeyColumns.get_comparison_q(
                key,
                primary_key_attribute,
                primary_key_attribute,
                Lookup.EXACT,
                value,
                accepts_instances=False,
                reads_outer_references=True,
            )
        if key != "pk__in":
            return None
        if isinstance(value, (QuerySpecification, Subquery, KeyRowsQuery)):
            return Q(pk__in=value)
        if not value:
            # The single-column form of an empty list matches no row - an empty OR would match all.
            empty_in_kwarg: dict[str, Any] = {f"{primary_key_attribute[0]}__in": []}
            return Q(**empty_in_kwarg)
        value_rows = list(value)
        # The shape of every row in one pass - a wrong row is reported by get_key_values().
        key_length = len(primary_key_attribute)
        if set(map(type, value_rows)) != {tuple} or set(map(len, value_rows)) != {key_length}:
            for item in value_rows:
                KeyColumns.get_key_values(key, primary_key_attribute, item, accepts_instances=False)
        return Q(pk__in=value_rows)

    @staticmethod
    def get_key_values(
        label: str,
        key_names: Sequence[str],
        item: Any,
        *,
        accepts_instances: bool = True,
        instance_key_names: Sequence[str] | None = None,
        reads_outer_references: bool = False,
    ) -> tuple[Any, ...]:
        """The values of one key value over several fields, its shape checked.

        Args:
            label: The filter kwarg, named in the error.
            key_names: The names of the key's fields, in key order.
            item: A tuple of the key's values, a model instance or an ``OuterReference``.
            accepts_instances: Whether a model instance names its key.
            instance_key_names: The instance's fields holding the key - its primary key when None.
            reads_outer_references: Whether an ``OuterReference`` names the outer row's key, component by
                component - ``OuterReference("pk")``/``OuterReference("rel__pk")`` the key fields themselves,
                ``OuterReference("<relation>")`` the key columns of a relation to the same target.

        Returns:
            The values, in key order.

        Raises:
            QueryError: The item is none of those, or a tuple of another length.
        """
        # Local imports: the models and expressions packages import this module.
        from hare.exceptions import QueryError
        from hare.models import Model
        from hare.query.expressions.subqueries.outer_reference import OuterReference

        if accepts_instances and isinstance(item, Model):
            if instance_key_names is not None:
                return tuple(RelationValues.get_relation_key_values(item, instance_key_names, f"Filter '{label}'"))
            item = item.pk
        elif reads_outer_references and isinstance(item, OuterReference):
            if item.field == "pk" or item.field.endswith("__pk"):
                path_prefix = item.field.removesuffix("pk")
                return tuple(OuterReference(f"{path_prefix}{key_name}") for key_name in key_names)
            return tuple(OuterReference(f"{item.field}_{key_name}") for key_name in key_names)
        if not isinstance(item, tuple) or len(item) != len(key_names):
            accepted = "a model instance or a" if accepts_instances else "a"
            raise QueryError(
                f"{label}= needs {accepted} {len(key_names)}-tuple matching {tuple(key_names)}, got {item!r}"
            )
        return item

    @staticmethod
    def get_comparison_q(
        label: str,
        field_names: Sequence[str],
        key_names: Sequence[str],
        lookup: str,
        value: Any,
        *,
        accepts_instances: bool = True,
        instance_key_names: Sequence[str] | None = None,
        reads_outer_references: bool = False,
    ) -> Q:
        """A comparison of a key over several fields, as comparisons of the fields - ``(a, b) =
        (x, y)`` is ``a = x AND b = y``, ``(a, b) <> (x, y)`` is ``a <> x OR b <> y``; a list is
        an OR of equalities for ``in``, an AND of inequalities for ``not_in``.

        Args:
            label: The filter kwarg, named in an error.
            field_names: The filter names of the key's fields, in key order.
            key_names: The names of the key's fields, in key order.
            lookup: ``""``, ``"not"``, ``"in"`` or ``"not_in"``.
            value: A key value, or a list of them for ``in``/``not_in``.
            accepts_instances: Whether a model instance names its key.
            instance_key_names: The instance's fields holding the key - its primary key when None.
            reads_outer_references: Whether an ``OuterReference`` names the outer row's key.

        Returns:
            The filter.

        Raises:
            QueryError: A key value isn't a model instance or a tuple of the key's length, or the
                value of ``in``/``not_in`` isn't a list.
        """
        # Local import: the expressions package imports this module.
        from hare.exceptions import QueryError
        from hare.query.enums import Connector, Lookup
        from hare.query.expressions.conditions.q import Q

        def get_key_values(item: Any) -> tuple[Any, ...]:
            return KeyColumns.get_key_values(
                label,
                key_names,
                item,
                accepts_instances=accepts_instances,
                instance_key_names=instance_key_names,
                reads_outer_references=reads_outer_references,
            )

        def get_equal_q(item: Any) -> Q:
            return Q(**dict(zip(field_names, get_key_values(item), strict=True)))

        def get_not_equal_q(item: Any) -> Q:
            return Q.with_connector(
                Connector.OR,
                *(
                    Q(**{f"{field_name}__not": key_value})
                    for field_name, key_value in zip(field_names, get_key_values(item), strict=True)
                ),
            )

        if lookup in {Lookup.IN, Lookup.NOT_IN} and not isinstance(value, (list, tuple, set)):
            accepted = "model instances or key tuples" if accepts_instances else "key tuples"
            raise QueryError(f"{label}= takes a list of {accepted}, got {value!r}")
        if lookup == Lookup.EXACT:
            return get_equal_q(value)
        if lookup == Lookup.NOT:
            return get_not_equal_q(value)
        items = list(value)
        if not items:
            # The single-column form of an empty list: `IN ()` matches no row, `NOT IN ()` every row.
            empty_list_kwarg: dict[str, Any] = {f"{field_names[0]}__{lookup}": []}
            return Q(**empty_list_kwarg)
        if lookup == Lookup.IN:
            return Q.with_connector(Connector.OR, *(get_equal_q(item) for item in items))
        return Q(*(get_not_equal_q(item) for item in items))
