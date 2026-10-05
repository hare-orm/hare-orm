from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions import Expression
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.constants import DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX
from hare.sql import Order, Table
from hare.sql.functions.json.json_sort_key import JsonSortKey
from hare.sql.terms.field import Field
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryOrdering:
    """The ORDER BY of a query: each ordering's terms - a relation's ordering key, an annotation, a
    path inside a value - with the placement of NULLs, and the ordering terms a DISTINCT needs
    selected."""

    @staticmethod
    def get_ordering(
        query: AwaitableQuery[Any],
        model: type[Model],
        table: Table,
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
        fields_for_select: Collection[str] | None = None,
        annotation_output_aliases: dict[str, str] | None = None,
        *,
        select_related_path_prefix: str = "",
    ) -> None:
        """
        Applies standard ordering to QuerySet.

        Args:
            query: The query.
            model: The Model this queryset is based on.
            table: ``hare.sql.Table`` to keep track of the virtual SQL table
                (to allow self referential joins)
            orderings: What columns/order to order by
            annotations: Annotations that may be ordered on
            fields_for_select: Contains fields that are selected in the SELECT clause if
                .only(), .values() or .values_list() are used.
            annotation_output_aliases: Maps an annotation's own name to the alias it is
                actually SELECTed under, for callers (``.values_list()``) that rename annotations to
                positional aliases (``"0"``, ``"1"``, ...) instead of keeping their own name in
                SELECT - an ORDER BY reference must use that same alias, since Postgres (unlike
                SQLite) rejects an ORDER BY alias that doesn't match a SELECTed column.
            select_related_path_prefix: The dotted relation path already walked to reach this
                call, empty at the top level - extended by one hop per recursive call below, so a
                multi-hop ordering (``left__left__name``) matches a ``Select(relation, extra_
                condition=...)`` registered under the FULL path (``"left__left"``), not just this
                hop's own name. Lets a JOIN built here for ordering fold in the SAME extra_condition
                ``SelectRelatedJoins.join_select_related()`` would otherwise apply later - without this,
                ``QueryJoins.join_table()``'s table-identity dedup keeps whichever JOIN got added FIRST, which
                would silently drop extra_condition if this method's own (otherwise unconditioned)
                JOIN ran first.

        Raises:
            FieldError: If a field provided does not exist in model.
        """
        orderings = query._apply_default_ordering(orderings, annotations)

        for ordering in orderings:
            field_name = ordering[0]
            key_field_names = QueryOrdering.get_relation_ordering_key_names(model, field_name)
            if key_field_names is not None:
                QueryOrdering.get_ordering(
                    query,
                    model,
                    table,
                    [(key_field_name, ordering[1]) for key_field_name in key_field_names],
                    annotations,
                    fields_for_select,
                    annotation_output_aliases,
                    select_related_path_prefix=select_related_path_prefix,
                )
                continue
            if field_name not in annotations and (
                field_name in model._meta.fetch_fields or (field_name == "pk" and "pk" not in model._meta.fields_map)
            ):
                # A to-many or reverse one-to-one relation orders by the related primary key,
                # "pk" by the primary key field(s), like Django.
                QueryOrdering.get_ordering(
                    query,
                    model,
                    table,
                    [(field_path, ordering[1]) for field_path in ConcreteFieldPaths.get_paths(model, field_name)],
                    annotations,
                    fields_for_select,
                    annotation_output_aliases,
                    select_related_path_prefix=select_related_path_prefix,
                )
                continue

            related_field_name, __, forwarded = field_name.partition("__")
            if related_field_name in model._meta.fetch_fields and field_name not in annotations:
                related_field = cast("RelationalField[Model]", model._meta.fields_map[related_field_name])
                full_path = (
                    f"{select_related_path_prefix}__{related_field_name}"
                    if select_related_path_prefix
                    else related_field_name
                )
                extra_condition = query._select_related_extra_conditions.get(full_path)
                related_table = QueryJoins.join_table_by_field(
                    query, table, related_field_name, related_field, extra_condition
                )
                QueryOrdering.get_ordering(
                    query,
                    related_field.related_model,
                    related_table,
                    [(forwarded, ordering[1])],
                    {},
                    select_related_path_prefix=full_path,
                )
            elif field_name in annotations:
                if model is query.model:
                    EncryptedFieldBase.raise_if_encrypted(
                        QueryAnnotations.get_field_object_by_path(query, field_name), "ORDER BY"
                    )
                term: Term
                # An ORDER BY uses the bare alias only when the annotation is selected: an .alias()
                # never is, unless .values()/.values_list() names it.
                is_selected = query.ordering_can_reference_annotation_alias and (
                    field_name not in query._alias_keys if not fields_for_select else field_name in fields_for_select
                )
                if is_selected:
                    alias = field_name
                    if annotation_output_aliases:
                        alias = annotation_output_aliases.get(field_name, field_name)
                    term = Field(alias)
                else:
                    term = QueryAnnotations.get_annotation_expression_term(query, field_name, annotations, table)
                if QueryAnnotations.annotation_holds_json(query, field_name, annotations, table):
                    # JSON values order as jsonb - by the expression itself, as a SELECT DISTINCT
                    # copies the ordering into SELECT, where no alias resolves.
                    term = JsonSortKey(
                        QueryAnnotations.get_annotation_expression_term(query, field_name, annotations, table)
                    )
                if model is query.model:
                    query._annotation_ordering_terms[field_name] = term
                query.query = query.query.orderby(term, order=ordering[1])
            else:
                field_object = model._meta.fields_map.get(field_name)

                if not field_object:
                    full_name = (
                        f"{select_related_path_prefix}__{field_name}" if select_related_path_prefix else field_name
                    )
                    raise FieldError(
                        f"Unknown field {full_name} for ordering: {model.__name__} has no field {field_name}"
                    )
                EncryptedFieldBase.raise_if_encrypted(field_object, "ORDER BY")
                field_name = field_object.source_field or field_name
                field: Term = table[field_name]

                function_cast = field_object.get_function_cast(query.dialect)
                if function_cast:
                    field = function_cast(field_object, field)
                if isinstance(field_object, JSONField):
                    # SQLite orders JSON values as Postgres orders jsonb.
                    field = JsonSortKey(field)

                query.query = query.query.orderby(field, order=ordering[1])

    @staticmethod
    def nulls_sort_first(query: AwaitableQuery[Any], order: Order) -> bool:
        """Whether NULLs of an ordering column sort first on this query's dialect: an explicit ``NULLS
        FIRST``/``NULLS LAST`` decides, else the dialect's default.

        Args:
            query: The query.
            order: The ordering's direction.
        """
        if order.nulls_first is not None:
            return order.nulls_first
        sorts_nulls_first = query.dialect.features.sorts_nulls_first
        return sorts_nulls_first if order.is_ascending else not sorts_nulls_first

    @staticmethod
    def include_orderbys_in_select(query: AwaitableQuery[Any]) -> None:
        """Adds every ORDER BY term missing from the SELECT list to it - a plain ``SELECT DISTINCT``
        needs them selected (Postgres). Applied on every dialect, so the rows are the same
        everywhere. A term added here is never read into a model attribute.

        Args:
            query: The query.
        """
        namespaced_context = query.query._sql_context_with_namespace(query.query.query_class.SQL_CONTEXT)
        selected_sql = set()
        for select_term in query.query._selects:
            selected_sql.add(select_term.get_sql(namespaced_context))
            # An annotation selected under an alias is ordered by a bare reference to the alias -
            # recognized as selected already, not appended again.
            if select_term.alias:
                selected_sql.add(Field(select_term.alias).get_sql(namespaced_context))
        orderbys: list[tuple[Term, Order | None]] = []
        for index, (term, direction) in enumerate(query.query._orderbys):
            term_sql = term.get_sql(namespaced_context)
            if term_sql not in selected_sql:
                if not isinstance(term, Field) and term.alias is None:
                    term = term.as_(f"{DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX}{index}")
                query.query = query.query.select(term)
                selected_sql.add(term_sql)
            orderbys.append((term, direction))
        query.query._orderbys = orderbys

    @staticmethod
    def get_relation_ordering_key_names(model: type[Model], field_name: str) -> tuple[str, ...] | None:
        """The key field names an ordering by a forward FK/O2O relation stands for, at the end of
        a plain or ``related__relation`` path.

        Args:
            model: The model the path starts from.
            field_name: An ordering field name.

        Returns:
            The relation's own key field names with the path prefix kept (``("tournament_id",)``,
            ``("event__tournament_id",)``), or None when the path doesn't end in a forward FK/O2O
            relation.
        """
        lookup_path = LookupPath.parse(model, field_name)
        if len(lookup_path.rest) != 1:
            return None
        meta = lookup_path.model._meta
        last_name = lookup_path.rest[0]
        if last_name not in meta.foreign_key_fields and last_name not in meta.one_to_one_fields:
            return None
        key_field_names = cast("RelationalField[Model]", meta.fields_map[last_name]).source_fields
        return tuple(f"{lookup_path.prefix}{key_field_name}" for key_field_name in key_field_names)
