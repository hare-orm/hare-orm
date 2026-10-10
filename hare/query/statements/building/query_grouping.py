from __future__ import annotations

from collections.abc import Iterator
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, UnSupportedError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins
from hare.sql.terms.field import Field
from hare.sql.terms.select_reference import SelectReference
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql.sql_context import SqlContext


class QueryGrouping:
    """The GROUP BY of a query: the grouping an aggregate annotation needs added on its own, the terms
    of the selected columns and of the ordering, each grouped once."""

    @staticmethod
    def apply_auto_group_by(query: AwaitableQuery[Any]) -> None:
        """Applies the implicit GROUP BY an aggregate annotation needs without an explicit
        ``.group_by()``: every selected non-aggregate term. Runs after every step adding joins or
        changing the SELECT list.

        With nothing non-aggregate selected, a HAVING filter groups by every base-table column - one
        group per row; without one the query is a single aggregate over the whole table and gets no
        GROUP BY. A ``.values()`` query whose aggregates were annotated before it groups by the
        primary key, like Django.

        Args:
            query: The query.
        """
        if query._has_aggregate:
            namespaced_context = query.query._sql_context_with_namespace(query.query.query_class.SQL_CONTEXT)
            grouped_sql: set[str] = set()
            group_by_terms: list[Term] = []
            for select_term in query.query._selects:
                QueryGrouping.add_group_by_terms(
                    group_by_terms,
                    grouped_sql,
                    QueryGrouping.get_select_group_by_terms(select_term),
                    namespaced_context,
                )
            QueryGrouping.add_group_by_terms(
                group_by_terms, grouped_sql, QueryGrouping.get_orderby_group_by_terms(query), namespaced_context
            )
            aggregates_per_model_row = query._aggregates_per_model_row()
            if not group_by_terms and not aggregates_per_model_row:
                if not query.query._havings:
                    return
                effective_table = query._effective_basetable()
                group_by_terms = [effective_table[field] for field in query.model._meta.db_fields]
            elif query.group_by_must_include_primary_key or aggregates_per_model_row:
                # One group per row: the selected columns alone needn't be unique, so the primary
                # key is grouped by too - it needn't be selected.
                effective_table = query._effective_basetable()
                QueryGrouping.add_group_by_terms(
                    group_by_terms,
                    grouped_sql,
                    [
                        effective_table[query.model._meta.fields_db_projection[attribute_name]]
                        for attribute_name in query.model._meta.primary_key_attribute_names
                    ],
                    namespaced_context,
                )
            if query.query._havings:
                # A column HAVING reads outside its aggregates (Q(n__gte=2) | Q(dept__name="ops"),
                # an aggregate compared with a column) must be grouped too - Postgres rejects it
                # otherwise, SQLite picks an arbitrary row's value.
                grouped_sql = {QueryGrouping.get_group_by_sql(term, namespaced_context) for term in group_by_terms}
                QueryGrouping.add_group_by_terms(
                    group_by_terms, grouped_sql, query.query._havings.get_group_by_column_terms(), namespaced_context
                )
            # Grouped by without their SELECT alias: a bare alias like "id" would be ambiguous next
            # to a joined table's column of that name.
            aliasless_terms = []
            for term in group_by_terms:
                # An alias-free term stays the same object, so an ORDER BY of it renders with the
                # same bind parameters as its GROUP BY.
                aliasless_term = term
                if term.alias is not None:
                    aliasless_term = copy(term)
                    aliasless_term.alias = None
                aliasless_terms.append(aliasless_term)
            query.query = query.query.groupby(*aliasless_terms)

    @staticmethod
    def get_select_group_by_terms(select_term: Term) -> list[Term]:
        """The GROUP BY terms one selected term needs. An expression grouped as a whole is referenced
        by its SELECT position - rendered again it would bind its literals under new placeholders.

        Args:
            select_term: The selected term.

        Returns:
            The terms to group by.
        """
        select_group_by_terms = select_term.get_group_by_terms()
        if (
            len(select_group_by_terms) == 1
            and select_group_by_terms[0] is select_term
            and not isinstance(select_term, Field)
        ):
            return [SelectReference(select_term)]
        return select_group_by_terms

    @staticmethod
    def get_orderby_group_by_terms(query: AwaitableQuery[Any]) -> list[Term]:
        """The ORDER BY terms the implicit GROUP BY also groups by - an ORDER BY column neither
        aggregated nor grouped is rejected by Postgres and collapses the result on SQLite.

        Args:
            query: The query.

        Returns:
            The group-by terms of every ORDER BY term.
        """
        orderby_group_by_terms: list[Term] = []
        for term, _direction in query.query._orderbys:
            # A bare table-less Field is a reference to a SELECT alias, which the selected terms
            # already cover.
            if isinstance(term, Field) and term.table is None:
                continue
            term_nodes: Iterator[Any] = term.nodes_()
            if any(node.is_subquery for node in term_nodes):
                # A subquery rendered again binds its literals again - Postgres wouldn't match it to
                # a GROUP BY copy, so the columns it reads are grouped instead.
                orderby_group_by_terms.extend(term.get_group_by_column_terms())
                continue
            orderby_group_by_terms.extend(term.get_group_by_terms())
        return orderby_group_by_terms

    @staticmethod
    def get_group_by_sql(term: Term, namespaced_context: SqlContext) -> str:
        """A group-by term's aliasless SQL - a term already grouped by is skipped by it.

        Args:
            term: The group-by term.
            namespaced_context: The query's namespaced SQL context.

        Returns:
            The SQL.
        """
        if isinstance(term, SelectReference):
            return term.get_sql(namespaced_context)
        aliasless_term = term
        if term.alias is not None:
            aliasless_term = copy(term)
            aliasless_term.alias = None
        return aliasless_term.get_sql(namespaced_context.copy(with_alias=False))

    @staticmethod
    def add_group_by_terms(
        group_by_terms: list[Term], grouped_sql: set[str], new_terms: list[Term], namespaced_context: SqlContext
    ) -> None:
        """Appends the terms whose SQL isn't grouped by yet.

        Args:
            group_by_terms: The GROUP BY terms collected so far, extended in place.
            grouped_sql: Their SQL, extended in place.
            new_terms: The candidate terms.
            namespaced_context: The query's namespaced SQL context.
        """
        for term in new_terms:
            term_sql = QueryGrouping.get_group_by_sql(term, namespaced_context)
            if term_sql not in grouped_sql:
                grouped_sql.add(term_sql)
                group_by_terms.append(term)

    @staticmethod
    def get_group_by_clause_terms(query: AwaitableQuery[Any], field_names: tuple[str, ...]) -> list[Term]:
        """The ``GROUP BY`` terms of the grouped names - with a ``Rollup``/``Cube``/``GroupingSets``,
        the names given apart first, then its element.

        Args:
            query: The query.
            field_names: The grouped names - the grouping set's among them.

        Returns:
            The terms.

        Raises:
            UnSupportedError: The query groups by a grouping set on a database without them.
        """
        grouping_set = query._grouping_set
        if grouping_set is None:
            return QueryGrouping.get_group_bys(query, *field_names)
        if not query._connection.features.supports_grouping_sets:
            raise UnSupportedError(
                f"{grouping_set!r} needs GROUP BY grouping sets, which {query._connection.dialect} doesn't have"
            )
        set_names = set(grouping_set.field_names)
        plain_names = [name for name in field_names if name not in set_names]
        terms_by_name = {name: QueryGrouping.get_group_bys(query, name) for name in grouping_set.field_names}
        return [*QueryGrouping.get_group_bys(query, *plain_names), grouping_set.get_element(terms_by_name)]

    @staticmethod
    def get_group_bys(query: AwaitableQuery[Any], *field_names: str) -> list[Term]:
        group_bys: list[Term] = []
        effective_table = query._effective_basetable()
        for field_name in field_names:
            EncryptedFieldBase.raise_if_encrypted(
                QueryAnnotations.get_field_object_by_path(query, field_name), "GROUP BY"
            )
            if field_name in query._annotations:
                annotation_term = QueryAnnotations.get_annotation_expression_term(
                    query, field_name, query._annotations, effective_table
                )
                if annotation_term.contains_aggregate:
                    raise FieldError(
                        f"Cannot group by {field_name!r} - it is an aggregate annotation, computed from the "
                        "groups themselves. Group by a field or a non-aggregate annotation instead."
                    )
                # A bare Field renders as the quoted SELECT alias. An annotation that is not
                # selected at all (.alias(), or left out of values()/values_list()) has no alias
                # to reference and is grouped by its full expression instead.
                select_alias = QueryAnnotations.get_annotation_select_alias(query, field_name)
                if select_alias is not None:
                    group_bys.append(Field(select_alias))
                else:
                    group_bys.append(
                        QueryAnnotations.get_annotation_expression_term(
                            query, field_name, query._annotations, effective_table
                        )
                    )
                continue
            # A composite key (pk, or a relation to one) groups by every one of its columns.
            for concrete_field_name in ConcreteFieldPaths.get_paths(query.model, field_name):
                field, __, forwarded_fields = concrete_field_name.partition("__")
                related_table, related_db_field = QueryJoins.join_table_with_forwarded_fields(
                    query,
                    model=query.model,
                    table=effective_table,
                    field=field,
                    forwarded_fields=forwarded_fields,
                )
                group_bys.append(
                    related_table[related_db_field].as_(f"{related_table.get_table_name()}__{concrete_field_name}")
                )
        return group_bys
