from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.query.expressions import Exists, Q, Subquery
from hare.query.expressions.joins.lateral import Lateral
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.subqueries.declarations import KeyRowsQuery
from hare.query.filters import FieldLookups
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.plans.description.plannable import Plannable
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.query_specification import QuerySpecification
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet


class FilterArguments:
    """The checks of what filter(), exclude() and the methods naming fields are given - known fields
    and lookups, names that are strings, no filter of a sliced queryset - made when the method is
    called."""

    @staticmethod
    def check_filter_keys(
        queryset: QuerySet[Any, Any], conditions: tuple[Q | Exists, ...], kwargs: dict[str, Any], method_name: str
    ) -> None:
        """Checks every filter key right away, through ``get_lookup_info()`` - the whole path
        through relations, the field and the lookup, or an annotation and its lookup.

        Args:
            queryset: The queryset.
            conditions: The ``Q``/``Exists`` conditions.
            kwargs: The keyword filters.
            method_name: ``filter`` or ``exclude``, named in the error.

        Raises:
            FieldError: A key names no field, relation or annotation, or a lookup its field doesn't
                have.
            QueryError: A key is a lookup other than equality, membership or ``isnull`` on a
                relation to a composite key.
        """
        keys = list(kwargs)
        pending_q_objects = [condition for condition in conditions if isinstance(condition, Q)]
        while pending_q_objects:
            q_object = pending_q_objects.pop()
            keys.extend(q_object.filters)
            pending_q_objects += [child for child in q_object.children if isinstance(child, Q)]
        meta = queryset.model._meta
        model_name = queryset.model.__name__
        annotations = queryset._annotations
        # The keys described already - the filters of a model use a few keys over and over.
        lookup_infos = meta.lookup_infos
        for key in keys:
            if not annotations and lookup_infos is not None and key in lookup_infos:
                continue
            label = f"{model_name}.objects.{method_name}({key}=...)"
            name, __, rest = key.partition("__")
            if name in annotations:
                if isinstance(queryset._annotations[name], NamedJoin):
                    # Checked against the related model when the filter is resolved.
                    continue
                # A path inside an annotation's value is only valid for a JSON value - the
                # annotation's type is resolved to tell, only for such a path.
                needs_output_fields = bool(rest) and rest not in FieldLookups.get(None)
                AnnotationArguments.get_annotation_description(
                    queryset,
                    "checked_filter",
                    key,
                    lambda: LookupInfoBuilder.get_lookup_info(
                        queryset.model,
                        key,
                        annotations=queryset._annotations,
                        annotation_fields=AnnotationArguments.get_annotation_output_fields(queryset)
                        if needs_output_fields
                        else None,
                        label=label,
                    ),
                )
            else:
                meta._get_lookup_info(key, label)

    @staticmethod
    def raise_if_not_names(queryset: QuerySet[Any, Any], names: Iterable[Any], method_name: str) -> None:
        """Rejects a name that isn't a string, before anything reads it as a path.

        Args:
            queryset: The queryset.
            names: The names given.
            method_name: The method, named in the error.

        Raises:
            QueryError: A name isn't a string.
        """
        for name in names:
            if not isinstance(name, str):
                raise QueryError(f"{queryset.model.__name__}.objects.{method_name}() takes field names, got {name!r}")

    @staticmethod
    def check_selected_names(queryset: QuerySet[Any, Any], names: Iterable[str], method_name: str) -> None:
        """Checks the first name of every selected path right away: a field, ``pk``, a relation
        or an annotation. The rest of a path is checked when the query is built.

        Args:
            queryset: The queryset.
            names: The selected paths.
            method_name: The method, named in the error.

        Raises:
            QueryError: A name isn't a string.
            FieldError: A path names no field of the model, or is a ``Lateral``'s name alone.
        """
        fields_map = queryset.model._meta.fields_map
        annotations = queryset._annotations
        for name in names:
            # A name that isn't a string fails on its first use below - told apart only then, so a
            # string name pays for no check.
            try:
                if name in annotations:
                    if isinstance(annotations[name], Lateral):
                        raise FieldError(
                            f"{queryset.model.__name__}.objects.{method_name}('{name}'): a Lateral is read through "
                            f"its columns - '{name}__<column>'"
                        )
                    continue
                first_name = name.partition("__")[0]
            except (TypeError, AttributeError):
                if isinstance(name, str):
                    raise
                raise QueryError(
                    f"{queryset.model.__name__}.objects.{method_name}() takes field names, got {name!r}"
                ) from None
            if first_name != "pk" and first_name not in fields_map and first_name not in annotations:
                model_name = queryset.model.__name__
                raise FieldError(
                    f"{model_name}.objects.{method_name}('{name}'): {model_name} has no field '{first_name}'"
                )

    @staticmethod
    def takes_pending_filters(queryset: QuerySet[Any, Any], kwargs: dict[str, Any]) -> bool:
        """Whether filter kwargs can be kept unbuilt - their conditions are plain ``Q`` objects, not
        ones built and checked over several key fields (a composite primary key, ``relation__pk``,
        a many-to-many relation) right when ``.filter()`` is called.

        Args:
            queryset: The queryset.
            kwargs: The filter kwargs.

        Returns:
            True when they can.
        """
        meta = queryset.model._meta
        primary_key_attribute = meta.primary_key_attribute
        # MetaInfo.has_composite_primary_key, read without the property's call - asked on every
        # filter() and get().
        if type(primary_key_attribute) is tuple and primary_key_attribute:
            return False
        many_to_many_fields = meta.many_to_many_fields
        for key in kwargs:
            # A relation's own key compared through its key fields: __pk, __pk__in.
            if key in many_to_many_fields or key[-4:] == "__pk" or key[-8:] == "__pk__in":
                return False
        return True

    @staticmethod
    def takes_pending_conditions(queryset: QuerySet[Any, Any], conditions: tuple[Any, ...]) -> bool:
        """Whether ``Q`` and ``Exists(...)`` conditions given to ``.filter()``/``.exclude()`` can be
        kept unbuilt - trees of filters each kept as ``takes_pending_filters()`` keeps a kwarg, of
        values describing themselves for a plan: no SQL term built by hand, no empty node.

        Args:
            queryset: The queryset.
            conditions: The positional conditions.

        Returns:
            True when they can.
        """
        declared_names = GenericForeignKeyFieldInstance.declared_names
        for condition in conditions:
            if isinstance(condition, Exists):
                continue
            if not isinstance(condition, Q):
                return False
            if declared_names and condition.names_generic_field(declared_names, queryset.model):
                return False
            if not FilterArguments.takes_pending_condition(queryset, condition):
                return False
        return True

    @staticmethod
    def takes_pending_condition(queryset: QuerySet[Any, Any], condition: Q) -> bool:
        """Whether one ``Q`` tree can be kept unbuilt - see ``takes_pending_conditions()``.

        Args:
            queryset: The queryset.
            condition: The tree.

        Returns:
            True when it can.
        """
        if condition.expression is not None:
            return isinstance(condition.expression, Plannable)
        if condition.children:
            for child in condition.children:
                if not FilterArguments.takes_pending_condition(queryset, child):
                    return False
            return True
        filters = condition.filters
        if not filters or not FilterArguments.takes_pending_filters(queryset, filters):
            return False
        for value in filters.values():
            # A SQL term built by hand describes itself by its rendered text alone.
            if isinstance(value, Term) and not isinstance(value, Plannable):
                return False
        return True

    @staticmethod
    def is_subquery_filter_value(value: Any) -> bool:
        """Whether a filter value is a query used as a subquery rather than bound values.

        Args:
            value: The filter value.

        Returns:
            True for a queryset, values query, union, ``Subquery`` or ``KeyRowsQuery``.
        """
        return isinstance(value, (QuerySpecification, Subquery, KeyRowsQuery))

    @staticmethod
    def raise_if_slice_taken(queryset: QuerySet[Any, Any], message: str) -> None:
        """Raises when the rows are a slice - changing the filters, the ordering or the distinct rows
        would change which rows the slice holds. A single-row queryset not taken from a slice is
        narrowed only when fetched.

        Args:
            queryset: The queryset.
            message: The error message.

        Raises:
            QueryError: The queryset is sliced.
        """
        if (queryset._limit is not None or queryset._offset) and (
            not queryset._single or queryset._is_single_row_of_slice
        ):
            raise QueryError(message)
