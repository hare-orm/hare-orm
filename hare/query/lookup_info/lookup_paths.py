"""Lookup paths - the ``__``-separated keys of ``.filter()`` and names of ``.order_by()`` turned
into the joins and the term a query reads when it is built (``LookupPaths``). Describing a key
without building a query is ``LookupInfoBuilder``'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.core.hare_context import HareContext
from hare.exceptions import FieldError, QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.lookup_info.value_paths import ValuePaths
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.scope_join_conditions import ScopeJoinConditions
from hare.sql import Table
from hare.sql.identifiers import Identifiers
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions import Q


class LookupPaths:
    """Resolves a `__`-lookup path (e.g. events__participants__name) into the joins and
    hare.sql term needed to filter/select on it."""

    #: The bytes a SELECT-list column label's qualifying prefix may take: the label is the join
    #: alias followed by ".{field_name}", and a join alias alone can already fill the identifier
    #: length limit - this smaller budget leaves room for the field name after it.
    MAX_SELECT_LABEL_PREFIX_BYTES = 20

    @staticmethod
    def safe_select_label(prefix: str, field_name: str) -> str:
        """Builds the ``"{prefix}.{field_name}"`` label of a ``select_related()`` column, shortened so
        the whole label fits the identifier length limit.
        """
        prefix = Identifiers.shorten(prefix, LookupPaths.MAX_SELECT_LABEL_PREFIX_BYTES)
        return f"{prefix}.{field_name}"

    @staticmethod
    def get_joins_for_related_field(
        table: Table,
        related_field: RelationalField[Model],
        related_field_name: str,
        filter_call_generation: int = 0,
    ) -> list[TableCriterionTuple]:
        """The JOINs from ``table`` through ``related_field`` - one for a forward or backward FK/O2O,
        the through table and the related table for a many-to-many.

        Args:
            table: The table the relation starts at.
            related_field: The relation.
            related_field_name: The relation's name on the model of ``table``.
            filter_call_generation: The ``.filter()``/``.exclude()`` call the JOIN is built for -
                part of a to-many relation's join alias, so a separate call gets a JOIN of its own.
                Zero for every other caller.

        Returns:
            The JOINs, each a table and its ON criterion.
        """
        if HareContext.get_current() is not None:
            owning_connection = related_field.model.get_connection(for_write=False)
            target_connection = related_field.related_model.get_connection(for_write=False)
            if owning_connection is not target_connection:
                raise QueryError(
                    f"{related_field.model.__name__}.{related_field_name} points at "
                    f"{related_field.related_model.__name__}, which resolves to a different "
                    "database connection - crossing it with a SQL JOIN (select_related()/.only()/"
                    ".filter()/.order_by()/.annotate() on this relation) isn't possible. Use "
                    "prefetch_related()/prefetch_related_objects() instead, which query each side's own "
                    "connection separately."
                )

        required_joins: list[TableCriterionTuple] = []
        generation_suffix = (
            f"__gen{filter_call_generation}" if filter_call_generation and related_field.is_multi_valued else ""
        )

        related_table: Table = related_field.related_model._meta.basetable
        if isinstance(related_field, ManyToManyFieldInstance):
            # Always aliased: a second many-to-many hop over the same through table would otherwise
            # count as already joined.
            through_table = Table(related_field.through, schema=related_field.through_schema).as_(
                Identifiers.get_within_limit(
                    f"{table.get_table_name()}__{related_field_name}__through{generation_suffix}"
                )
            )
            related_table = related_table.as_(
                Identifiers.get_within_limit(f"{table.get_table_name()}__{related_field_name}{generation_suffix}")
            )
            owner_pk_columns = KeyColumns.get_source_columns(related_field.model._meta)
            target_pk_columns = KeyColumns.get_source_columns(related_field.related_model._meta)
            required_joins.append(
                (
                    through_table,
                    KeyColumns.row_equality(
                        [table[column] for column in owner_pk_columns],
                        [through_table[column] for column in related_field.backward_keys],
                    ),
                )
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [through_table[column] for column in related_field.forward_keys],
                        [related_table[column] for column in target_pk_columns],
                    ),
                )
            )
        elif isinstance(related_field, BackwardForeignKeyRelation):
            to_field_source_fields = [
                to_field.source_field or to_field.model_field_name for to_field in related_field.to_field_instances
            ]

            # Always aliased: a self-referential relation walked several hops deep can collide with
            # any earlier hop or with the FROM table.
            related_table = related_table.as_(
                Identifiers.get_within_limit(f"{table.get_table_name()}__{related_field_name}{generation_suffix}")
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [table[column] for column in to_field_source_fields],
                        [related_table[column] for column in related_field.relation_source_fields],
                    ),
                )
            )
        else:
            to_field_source_fields = [
                to_field.source_field or to_field.model_field_name for to_field in related_field.to_field_instances
            ]

            from_fields = [related_field.model._meta.fields_map[sf] for sf in related_field.source_fields]
            from_field_source_fields = [to_field.source_field or to_field.model_field_name for to_field in from_fields]

            related_table = related_table.as_(
                Identifiers.get_within_limit(f"{table.get_table_name()}__{related_field_name}")
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [related_table[column] for column in to_field_source_fields],
                        [table[column] for column in from_field_source_fields],
                    ),
                )
            )
        return required_joins

    @staticmethod
    def expand_expression(root_model: type[Model], lookup_expression: str) -> Sequence[Field[Any]]:
        lookup_path = LookupPath.parse(root_model, lookup_expression)
        if len(lookup_path.rest) != 1:
            raise FieldError(f"{lookup_expression} not resolvable")
        model = lookup_path.model
        last_field_name = lookup_path.rest[0]
        if last_field_name == "pk" and isinstance(model._meta.primary_key_attribute, str):
            last_field_name = model._meta.primary_key_attribute
        last_field = model._meta.fields_map.get(last_field_name)
        if last_field is None:
            raise FieldError(f"{lookup_expression} not resolvable")
        return [*lookup_path.relations, last_field]

    @staticmethod
    def get_scoped_joins(
        table: Table,
        related_field: RelationalField[Model],
        related_field_name: str,
        *,
        visibility: RowVisibility,
        extra_condition: Q | None = None,
        filter_call_generation: int = 0,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> list[TableCriterionTuple]:
        """The JOINs of one relation hop scoped like the related model's own queryset: a through
        model's default scope and the visibility of the rows it points at, the related model's
        default scope, and a ``Select(relation, extra_condition=...)`` of the hop - the one place a
        JOIN to a model gets its scope.

        Args:
            table: The table the hop starts from.
            related_field: The relation crossed.
            related_field_name: The relation's name on the model of ``table``.
            visibility: Which rows the default scopes let the query see.
            extra_condition: A ``Select(relation, extra_condition=...)`` condition of the hop - a
                query with one keeps no plan.
            filter_call_generation: As in ``get_joins_for_related_field()``.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.

        Returns:
            The joins, the related table last.
        """
        joins = LookupPaths.get_joins_for_related_field(
            table,
            related_field,
            related_field_name,
            filter_call_generation,
        )
        ScopeJoinConditions.fold_through_join_scopes(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        ScopeJoinConditions.fold_ambient_scope_into_join(
            joins,
            related_field.related_model,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        ScopeJoinConditions.fold_extra_condition_into_join(
            joins,
            related_field.related_model,
            extra_condition,
            dialect=dialect,
            connection=connection,
        )
        ScopeJoinConditions.fold_extra_condition_into_through_join(
            joins,
            related_field,
            extra_condition,
            dialect=dialect,
            connection=connection,
        )
        return joins

    @staticmethod
    def get_nested_field(
        model: type[Model],
        table: Table,
        field: str,
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        select_related_extra_conditions: Mapping[str, Q] | None = None,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> tuple[Term, list[TableCriterionTuple], Field[Any] | None]:
        """
        Resolves a nested field string like events__participants__name and
        returns the hare.sql term, required joins and the Field that can be used for
        converting the value.

        Args:
            model: The model to resolve ``field`` against.
            table: The table ``model`` is aliased to in the query being built.
            field: The dotted nested field path.
            visibility: Which rows the default scopes let the query see.
            select_related_extra_conditions: The originating queryset's own
                ``_select_related_extra_conditions``, keyed by dotted relation path - folded into
                the matching JOIN the same way ``RelationFilters.get_nested_filter()`` already does, so
                ``F("relation__field")``/``Count("relation__field")``/a window function crossing
                a relation respects a ``Select(relation, extra_condition=...)`` declared for it
                instead of silently ignoring it.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on; None when compiling for no particular
                database.
        """
        if (path_split := ValuePaths.get_value_path_split(model, field)) is not None:
            field_path, path_field, path_segments = path_split
            term, joins, __ = LookupPaths.get_nested_field(
                model,
                table,
                field_path,
                visibility=visibility,
                select_related_extra_conditions=select_related_extra_conditions,
                dialect=dialect,
                connection=connection,
            )
            term, output_field = ValuePaths.get_value_path_term(term, path_field, path_segments, field)
            return term, joins, output_field
        joins = []
        fields = LookupPaths.expand_expression(model, field)
        path: str | None = None

        for iter_field in fields[:-1]:
            related_field = cast("RelationalField[Model]", iter_field)
            path = iter_field.model_field_name if path is None else f"{path}__{iter_field.model_field_name}"
            new_joins = LookupPaths.get_scoped_joins(
                table,
                related_field,
                related_field.model_field_name,
                visibility=visibility,
                extra_condition=select_related_extra_conditions.get(path) if select_related_extra_conditions else None,
                dialect=dialect,
                connection=connection,
            )
            joins.extend(new_joins)

            model = related_field.related_model
            # The table the join was built against, with whatever alias it got.
            table = new_joins[-1][0]

        last_field = fields[-1]
        if last_field.model_field_name in model._meta.fetch_fields:
            related_field = cast("RelationalField[Model]", last_field)
            related_field_meta = related_field.related_model._meta
            path = last_field.model_field_name if path is None else f"{path}__{last_field.model_field_name}"

            new_joins = LookupPaths.get_scoped_joins(
                table,
                related_field,
                related_field.model_field_name,
                visibility=visibility,
                extra_condition=select_related_extra_conditions.get(path) if select_related_extra_conditions else None,
                dialect=dialect,
                connection=connection,
            )
            joins.extend(new_joins)
            # Same reasoning as above: the table actually joined (possibly aliased) is the one
            # the terminal field's value must be read from, not a freshly-fetched, always-
            # unaliased meta.basetable.
            related_table = new_joins[-1][0]

            if related_field_meta.has_composite_primary_key:
                # A composite primary key is no single column - the term is used as one value.
                raise QueryError(
                    f"'{field}' ends on a relation to a composite primary key - there's no single "
                    "column to resolve. Reference the individual components instead, e.g. "
                    f"'{field}__{related_field_meta.primary_key_attribute_names[0]}'."
                )
            if related_field_meta.has_primary_key:
                term = related_table[related_field_meta.db_pk_column]
            else:
                # A related model without a primary key is read by its key column to this row -
                # never NULL in a joined row, like a primary key.
                term = related_table[cast("BackwardForeignKeyRelation[Model]", last_field).relation_source_fields[0]]
        else:
            term = table[last_field.source_field or last_field.model_field_name]

            if last_field:  # pragma: nobranch
                function_cast = last_field.get_function_cast(dialect)
                if function_cast:
                    term = function_cast(last_field, term)

        return term, joins, last_field
