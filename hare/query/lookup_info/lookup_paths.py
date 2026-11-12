"""Lookup paths - the ``__``-separated keys of ``.filter()`` and names of ``.order_by()`` turned
into the joins and the term a query reads when it is built (``LookupPaths``). Describing a key
without building a query is ``LookupInfoBuilder``'s.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.scope_join_conditions import ScopeJoinConditions
from hare.sql import Table
from hare.sql.identifiers import Identifiers
from hare.sql.terms.field import Field as SqlField
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions import Q
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences


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
    def crosses_connections(related_field: RelationalField[Any]) -> bool:
        """Whether a relation joins its model to one on another connection.

        This compares routing metadata without requiring an ambient tenant. The selected
        execution client is checked when a query or relation operation uses its tables.

        Args:
            related_field: The relation.

        Returns:
            True when the two models resolve to different connections; False outside a Hare context, and for a
            model of no connection.
        """
        if HareContext.get_current() is None:
            return False
        try:
            connection = ModelConnectionChoice.get_metadata_connection(related_field.model)
            related_connection = ModelConnectionChoice.get_metadata_connection(related_field.related_model)
        except ConfigurationError:
            # A model of no connection - one of a migration's state, its DDL written - crosses none; a query of
            # it fails asking for its connection.
            return False
        return connection is not related_connection

    @staticmethod
    def holds_through_table(related_field: RelationalField[Model]) -> bool:
        """Whether a many-to-many relation's through table lives on the connection of the model the
        relation is crossed from - the model declaring the field keeps it.

        Args:
            related_field: The relation.

        Returns:
            True for a many-to-many relation crossed from the model declaring it, or from a model on
            that model's connection.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return False
        declaring_model = related_field.related_model if related_field._generated else related_field.model
        declaring_connection = ModelConnectionChoice.get_metadata_connection(declaring_model)
        return declaring_connection is ModelConnectionChoice.get_metadata_connection(related_field.model)

    @staticmethod
    def get_joins_for_related_field(
        table: Table,
        related_field: RelationalField[Model],
        related_field_name: str,
        filter_call_generation: int = 0,
        *,
        through_only: bool = False,
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
            through_only: The JOIN of a many-to-many relation's through table alone - a filter on
                the relation's links, which needs no row of the related table.

        Returns:
            The JOINs, each a table and its ON criterion.

        Raises:
            QueryError: The related model is on another connection, and the JOIN needs its table.
        """
        if LookupPaths.crosses_connections(related_field) and not (
            through_only and LookupPaths.holds_through_table(related_field)
        ):
            raise QueryError(
                f"{related_field.model.__name__}.{related_field_name} points at "
                f"{related_field.related_model.__name__}, which resolves to a different "
                "database connection - crossing it with a SQL JOIN (select_related()/.only()/"
                ".filter()/.order_by()/.annotate() on this relation) isn't possible. Use "
                "prefetch_related()/prefetch_related_objects() instead, which query each side's own "
                "connection separately; a many-to-many relation is read through its relation "
                "(obj.tags.all()), and filtered by its links from the model declaring it "
                "(tags=..., tags__in=..., tags__isnull=...)."
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
            if through_only:
                return required_joins
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
        """The fields a lookup path crosses, from the model to the field it ends at.

        Args:
            root_model: The model the path starts at.
            lookup_expression: The path - ``author__name``.

        Returns:
            The fields.

        Raises:
            FieldError: The path names no field.
        """
        lookup_path = LookupPath.parse(root_model, lookup_expression)
        if len(lookup_path.rest) != 1:
            raise FieldError(f"{lookup_expression} not resolvable")
        model = lookup_path.model
        last_field_name = lookup_path.rest[0]
        if last_field_name == PRIMARY_KEY_ALIAS and isinstance(model._meta.primary_key_attribute, str):
            last_field_name = model._meta.primary_key_attribute
        last_field = model._meta.fields_map.get(last_field_name)
        if last_field is None:
            raise FieldError(f"Unknown field {lookup_expression!r}: {model.__name__} has no field {last_field_name!r}")
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
        through_only: bool = False,
        value_wrapper_references: RecordedValueReferences | None = None,
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
            extra_condition: A ``Select(relation, extra_condition=...)`` condition of the hop.
            filter_call_generation: As in ``get_joins_for_related_field()``.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
            through_only: The JOIN of a many-to-many relation's through table alone, under the through
                model's default scope - the related model's own scope can't be read without its
                table.
            value_wrapper_references: An existing recorder for the extra condition; otherwise
                the active statement recorder receives its references.

        Returns:
            The joins, the related table last.
        """
        joins = LookupPaths.get_joins_for_related_field(
            table,
            related_field,
            related_field_name,
            filter_call_generation,
            through_only=through_only,
        )
        if through_only:
            ScopeJoinConditions.fold_through_model_ambient_scope_into_join(
                joins, related_field, visibility=visibility, dialect=dialect, connection=connection
            )
            return joins
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
            value_wrapper_references=value_wrapper_references,
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
    def mark_sensitive_column(term: Term, field: Field[Any] | None) -> None:
        """Marks the column a term reads as a sensitive field's (``Term.sensitive``) - a filter comparing
        an expression over it binds its values hidden, as one on the field does.

        Args:
            term: The term reading the field's column.
            field: The field read; None for a value of no field.
        """
        if field is not None and field.sensitive:
            node: Any
            for node in term.nodes_():
                if isinstance(node, SqlField):
                    node.sensitive = True
