"""Lookup paths - the ``__``-separated keys of ``.filter()`` and names of ``.order_by()`` turned
into the joins and the term a query reads when it is built (``LookupPaths``). Describing a key
without building a query is ``LookupInfoBuilder``'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.core.lookup_path import LookupPath
from hare.dialects.identifiers import Identifiers
from hare.exceptions import FieldError, QueryError
from hare.fields.base.field import Field
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.composite import KeyColumns
from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.filters.constants import (
    CALENDAR_DATE_PART_LOOKUPS,
    DATETIME_CAST_SEGMENTS,
    DATETIME_DATE_PART_LOOKUPS,
    TIME_OF_DAY_DATE_PART_LOOKUPS,
)
from hare.query.filters.field_transforms import FieldTransforms
from hare.query.filters.json_filter_parser import JsonFilterParser
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Table
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.base.term import Term
from hare.sql.terms.star import Star

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
        *,
        dialect: Dialect,
        connection: DatabaseClient | None,
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
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.

        Returns:
            The JOINs, each a table and its ON criterion.
        """
        # A JOIN between models on different connections can't run - rejected here rather than by
        # the database's "no such table". Checked only with an active context.
        from hare.core.context import HareContext

        if HareContext.get_current() is not None:
            owning_db = related_field.model.get_connection(for_write=False)
            target_db = related_field.related_model.get_connection(for_write=False)
            if owning_db is not target_db:
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
                        [table[c] for c in owner_pk_columns],
                        [through_table[c] for c in related_field.backward_keys],
                    ),
                )
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [through_table[c] for c in related_field.forward_keys],
                        [related_table[c] for c in target_pk_columns],
                    ),
                )
            )
        elif isinstance(related_field, BackwardFKRelation):
            to_field_source_fields = [f.source_field or f.model_field_name for f in related_field.to_field_instances]

            # Always aliased: a self-referential relation walked several hops deep can collide with
            # any earlier hop or with the FROM table.
            related_table = related_table.as_(
                Identifiers.get_within_limit(f"{table.get_table_name()}__{related_field_name}{generation_suffix}")
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [table[c] for c in to_field_source_fields],
                        [related_table[c] for c in related_field.relation_source_fields],
                    ),
                )
            )
        else:
            to_field_source_fields = [f.source_field or f.model_field_name for f in related_field.to_field_instances]

            from_fields = [related_field.model._meta.fields_map[sf] for sf in related_field.source_fields]
            from_field_source_fields = [f.source_field or f.model_field_name for f in from_fields]

            related_table = related_table.as_(
                Identifiers.get_within_limit(f"{table.get_table_name()}__{related_field_name}")
            )
            required_joins.append(
                (
                    related_table,
                    KeyColumns.row_equality(
                        [related_table[c] for c in to_field_source_fields],
                        [table[c] for c in from_field_source_fields],
                    ),
                )
            )
        return required_joins

    @staticmethod
    def get_value_path_split(model: type[Model], name: str) -> tuple[str, Field[Any], list[str]] | None:
        """Splits a name reading a path inside a field's value - a JSON key path
        (``data__owner``) or an array/range transform (``tags__0``, ``during__startswith``),
        after any relations (``crates__tags__0``).

        Args:
            model: The model the name starts at.
            name: The name.

        Returns:
            The field's own path, the field, and the path segments inside its value - or None
            when the name doesn't read inside a JSON, array or range field.
        """
        lookup_path = LookupPath.parse(model, name)
        if len(lookup_path.rest) < 2:
            return None
        field_name, *path_segments = lookup_path.rest
        field = lookup_path.model._meta.fields_map.get(field_name)
        if field is None:
            return None
        field_path = f"{lookup_path.prefix}{field_name}"
        if isinstance(field, JSONField):
            return field_path, field, path_segments
        transforms, __, rest_segments = FieldTransforms.get_path(field, path_segments)
        if transforms and not rest_segments:
            return field_path, field, path_segments
        return None

    @staticmethod
    def get_date_part_segments(field: Field[Any]) -> frozenset[str]:
        """The segments reading a part of a date, time or datetime field's value - a datetime's
        date parts, ``date`` and ``time``, a date's calendar parts, a time's time-of-day parts.

        Args:
            field: The field.

        Returns:
            The segments; empty for any other field.
        """
        effective_field = FieldTransforms.get_effective_field(field)
        if isinstance(effective_field, DatetimeField):
            return DATETIME_CAST_SEGMENTS | frozenset(DATETIME_DATE_PART_LOOKUPS)
        if isinstance(effective_field, DateField):
            return frozenset(CALENDAR_DATE_PART_LOOKUPS)
        if isinstance(effective_field, TimeField):
            return frozenset(TIME_OF_DAY_DATE_PART_LOOKUPS)
        return frozenset()

    @classmethod
    def get_date_part_split(cls, model: type[Model], name: str) -> tuple[str, str] | None:
        """Splits a name reading a part of a date, time or datetime field - ``created__year``,
        ``created__date``, ``starts__hour`` - after any relations (``tournament__created__month``).

        Args:
            model: The model the name starts at.
            name: The name.

        Returns:
            The field's own path and the part, or None when the name reads no such part.
        """
        lookup_path = LookupPath.parse(model, name)
        if len(lookup_path.rest) != 2:
            return None
        field_name, part = lookup_path.rest
        field = lookup_path.model._meta.fields_map.get(field_name)
        if field is None or field_name in lookup_path.model._meta.fetch_fields:
            return None
        if part not in cls.get_date_part_segments(field):
            return None
        return f"{lookup_path.prefix}{field_name}", part

    @staticmethod
    def get_value_path_term(
        term: Term, field: Field[Any], path_segments: list[str], name: str
    ) -> tuple[Term, Field[Any]]:
        """The term reading a path inside a field's value.

        Args:
            term: The field's column term.
            field: The JSON, array or range field.
            path_segments: The path inside its value.
            name: The whole name, for the error message.

        Returns:
            The term and the field of the value it reads.

        Raises:
            FieldError: If the field is an encrypted JSON field.
        """
        if isinstance(field, JSONField):
            if isinstance(field, EncryptedJSONField):
                raise FieldError(
                    f"{field.get_field_label()} is encrypted - {name!r} would read a stored Fernet token, "
                    "not the key's value. Reference the whole field instead."
                )
            key_parts = JsonFilterParser.get_key_parts("__".join(path_segments))
            return JsonFilterParser.get_field_path(term, key_parts, as_text=False), field.get_path_value_field()
        transforms, output_field, __ = FieldTransforms.get_path(field, path_segments)
        return FieldTransforms.apply(transforms, term), output_field

    @staticmethod
    def expand_expression(root_model: type[Model], lookup_expression: str) -> Sequence[Field[Any]]:
        lookup_path = LookupPath.parse(root_model, lookup_expression)
        if len(lookup_path.rest) != 1:
            raise FieldError(f"{lookup_expression} not resolvable")
        model = lookup_path.model
        last_field_name = lookup_path.rest[0]
        if last_field_name == "pk" and isinstance(model._meta.pk_attr, str):
            last_field_name = model._meta.pk_attr
        last_field = model._meta.fields_map.get(last_field_name)
        if last_field is None:
            raise FieldError(f"{lookup_expression} not resolvable")
        return [*lookup_path.relations, last_field]

    @staticmethod
    def _fold_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_model: type[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs the related model's default scope into ``joins[-1]``, in place - a JOIN never goes
        through the model's manager.

        Args:
            joins: The joins; the last one targets ``related_model``.
            related_model: The related model.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
        """
        # Local import: hare.query.manager imports QuerySet, which eventually imports this module
        # at module level - importing it back here at module level would be circular.
        from hare.query.scopes.row_scopes import RowScopes

        join_table, join_criterion = joins[-1]
        ambient_criterion = RowScopes.of(related_model).get_criterion(
            join_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if ambient_criterion is not None:
            joins[-1] = (join_table, join_criterion & ambient_criterion)

    @staticmethod
    def _fold_through_model_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs a through model's own default scope into ``joins[0]`` of a many-to-many hop, in place -
        a soft-deleted through row is no link. A no-op for any other relation and for a through
        table without a model.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        through_model = related_field.through_model_class
        if through_model is None:
            return
        # Local import: same circularity reasoning as _fold_ambient_scope_into_join() above.
        from hare.query.scopes.row_scopes import RowScopes

        join_table, join_criterion = joins[0]
        ambient_criterion = RowScopes.of(through_model).get_criterion(
            join_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if ambient_criterion is not None:
            joins[0] = (join_table, join_criterion & ambient_criterion)

    @staticmethod
    def _fold_visible_target_into_through_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs into ``joins[0]`` of a many-to-many hop an ``EXISTS`` that the linked row is visible in
        the related model's default scope - a through row pointing at a hidden row joins nothing.

        Args:
            joins: The through-table join followed by the related-table join; changed in place.
            related_field: The relation; anything but a many-to-many field is left unchanged.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        # Local imports: hare.query.manager and hare.query.expressions.subquery both import this
        # module at module level.
        from hare.query.expressions.exists_term import ExistsTerm
        from hare.query.scopes.row_scopes import RowScopes

        through_table, through_criterion = joins[0]
        related_meta = related_field.related_model._meta
        visible_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{through_table.get_table_name()}__visible")
        )
        visible_criterion = RowScopes.of(related_field.related_model).get_criterion(
            visible_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if visible_criterion is None:
            return
        link_criterion = KeyColumns.row_equality(
            [through_table[column] for column in related_field.forward_keys],
            [visible_table[column] for column in KeyColumns.get_source_columns(related_meta)],
        )
        visible_exists = ExistsTerm(
            QueryBuilder().from_(visible_table).select(Star()).where(link_criterion & visible_criterion)
        )
        joins[0] = (through_table, through_criterion & visible_exists)

    @staticmethod
    def _fold_extra_condition_into_join(
        joins: list[TableCriterionTuple],
        related_model: type[Model],
        extra_condition: Q | None,
        *,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs a ``Select(relation, extra_condition=Q(...))`` declared for the relation into
        ``joins[-1]``, in place - the JOIN built first is the one kept.
        """
        if extra_condition is None:
            return
        # Local imports: ExpressionContext's own module and the plans package import this one.
        from hare.query.expressions.base.expression_context import ExpressionContext
        from hare.query.plans.join_condition_recording import JoinConditionRecording

        JoinConditionRecording.record_unbindable()

        join_table, join_criterion = joins[-1]
        extra_modifier = extra_condition.get_result(
            ExpressionContext(
                model=related_model,
                table=join_table,
                annotations={},
                dialect=dialect,
                connection=connection,
            )
        )
        if extra_modifier.joins:
            raise QueryError(
                "Select(relation, extra_condition=...) only supports direct fields of the related model, "
                "not a further relation"
            )
        if extra_modifier.where_criterion:
            joins[-1] = (join_table, join_criterion & extra_modifier.where_criterion)

    @staticmethod
    def _fold_through_join_scopes(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """Folds into the joins of one relation hop the default scope of a real through model
        (``_fold_through_model_ambient_scope_into_join()``) and the visibility of the rows its
        through rows point at (``_fold_visible_target_into_through_join()``).

        Args:
            joins: The joins of the hop, changed in place.
            related_field: The relation crossed.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
        """
        LookupPaths._fold_through_model_ambient_scope_into_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        LookupPaths._fold_visible_target_into_through_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )

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
            dialect=dialect,
            connection=connection,
        )
        LookupPaths._fold_through_join_scopes(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        LookupPaths._fold_ambient_scope_into_join(
            joins,
            related_field.related_model,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        LookupPaths._fold_extra_condition_into_join(
            joins,
            related_field.related_model,
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
                the matching JOIN the same way ``Q._get_nested_filter()`` already does, so
                ``F("relation__field")``/``Count("relation__field")``/a window function crossing
                a relation respects a ``Select(relation, extra_condition=...)`` declared for it
                instead of silently ignoring it.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on; None when compiling for no particular
                database.
        """
        if (path_split := LookupPaths.get_value_path_split(model, field)) is not None:
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
            term, output_field = LookupPaths.get_value_path_term(term, path_field, path_segments, field)
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
                    f"'{field}__{related_field_meta.pk_attr_names[0]}'."
                )
            if related_field_meta.has_primary_key:
                term = related_table[related_field_meta.db_pk_column]
            else:
                # A related model without a primary key is read by its key column to this row -
                # never NULL in a joined row, like a primary key.
                term = related_table[cast("BackwardFKRelation[Model]", last_field).relation_source_fields[0]]
        else:
            if last_field.source_field:
                term = table[last_field.source_field]
            else:
                term = table[last_field.model_field_name]

            if last_field:  # pragma: nobranch
                func = last_field.get_function_cast(dialect)
                if func:
                    term = func(last_field, term)

        return term, joins, last_field
