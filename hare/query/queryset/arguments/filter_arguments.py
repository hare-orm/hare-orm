from __future__ import annotations

from collections.abc import Container, Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import FieldError, QueryError
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions import Exists, Q, Subquery
from hare.query.expressions.constants import CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES, PLAIN_VALUE_TYPES
from hare.query.expressions.joins.lateral import Lateral
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.subqueries.declarations import KeyRowsQuery
from hare.query.filters import FieldLookups
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.plans.constants import CALL_SIGNATURE_DESCRIBED_VALUE_TYPES, SELECT_FOR_UPDATE_OPTION_NAMES
from hare.query.plans.description.plannable import Plannable
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.query.queryset.queryset import QuerySet


class FilterArguments:
    """The checks of what filter(), exclude() and the methods naming fields are given - known fields
    and lookups, names that are strings, no filter of a sliced queryset - made when the method is
    called."""

    #: A direct get permits row-lock settings; every other option must remain unset.
    non_lock_option_names: ClassVar[tuple[str, ...]] = tuple(
        name for name in QueryOptions.DEFAULTS if name not in SELECT_FOR_UPDATE_OPTION_NAMES
    )

    @staticmethod
    def check_filter_keys(queryset: QuerySet[Any, Any], conditions: tuple[Q | Exists, ...], method_name: str) -> None:
        """Checks every filter key of ``Q`` conditions right away (``check_filter_key()``); the keyword
        filters are checked by ``get_checked_filters()``.

        Args:
            queryset: The queryset.
            conditions: The ``Q``/``Exists`` conditions.
            method_name: ``filter`` or ``exclude``, named in the error.
        """
        keys: list[str] = []
        pending_q_objects = [condition for condition in conditions if isinstance(condition, Q)]
        while pending_q_objects:
            q_object = pending_q_objects.pop()
            keys.extend(q_object.filters)
            pending_q_objects += [child for child in q_object.children if isinstance(child, Q)]
        # The keys described already - the filters of a model use a few keys over and over.
        lookup_infos = None if queryset._annotations else LookupInfoBuilder.lookup_infos.get_bucket(queryset.model)
        for key in keys:
            if lookup_infos is None or key not in lookup_infos:
                FilterArguments.check_filter_key(queryset, key, method_name)

    @staticmethod
    def get_checked_filters(
        queryset: QuerySet[Any, Any], kwargs: dict[str, Any], method_name: str
    ) -> tuple[dict[str, Any], bool]:
        """The keyword filters of a ``filter()``/``exclude()`` call, in one pass over them: each key
        checked (``check_filter_key()``), each value taken as it is now - a list, set or dict copied,
        an iterable ``__in`` value read once into a list - and whether the call can be kept unbuilt.

        Args:
            queryset: The queryset.
            kwargs: The keyword filters.
            method_name: ``filter`` or ``exclude``, named in the error.

        Returns:
            The filters, and whether none of them is built and checked right away
            (``takes_pending_filters()``).
        """
        meta = queryset.model._meta
        # The keys described already - the filters of a model use a few keys over and over.
        lookup_infos = None if queryset._annotations else LookupInfoBuilder.lookup_infos.get_bucket(queryset.model)
        many_to_many_fields = meta.many_to_many_fields
        primary_key_attribute = meta.primary_key_attribute
        # MetaInfo.has_composite_primary_key, read without the property's call.
        takes_pending = not (type(primary_key_attribute) is tuple and primary_key_attribute)
        filters: dict[str, Any] = {}
        for key, value in kwargs.items():
            if lookup_infos is None or key not in lookup_infos:
                FilterArguments.check_filter_key(queryset, key, method_name)
            value = filters[key] = FilterValues.get_list_lookup_value(key, FilterValues.get_detached_value(value))
            if takes_pending and FilterArguments.is_built_at_call(many_to_many_fields, key, value):
                takes_pending = False
        return filters, takes_pending

    @staticmethod
    def check_filter_key(queryset: QuerySet[Any, Any], key: str, method_name: str) -> None:
        """Checks a filter key right away, through ``get_lookup_info()`` - the whole path through
        relations, the field and the lookup, or an annotation and its lookup.

        Args:
            queryset: The queryset.
            key: The filter key.
            method_name: ``filter`` or ``exclude``, named in the error.

        Raises:
            FieldError: The key names no field, relation or annotation, or a lookup its field doesn't
                have.
            QueryError: The key is a lookup other than equality, membership or ``isnull`` on a
                relation to a composite key.
        """
        label = f"{queryset.model.__name__}.objects.{method_name}({key}=...)"
        name, __, rest = key.partition("__")
        annotations = queryset._annotations
        if name in annotations:
            if isinstance(annotations[name], NamedJoin):
                # Checked against the related model when the filter is resolved.
                return
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
                    annotations=annotations,
                    annotation_fields=AnnotationArguments.get_annotation_output_fields(queryset)
                    if needs_output_fields
                    else None,
                    label=label,
                ),
            )
        else:
            queryset.model._meta._get_lookup_info(key, label)

    @staticmethod
    def raise_if_not_names(queryset: QuerySet[Any, Any], names: Iterable[Any], method_name: str) -> None:
        """Rejects a name that isn't a string, before anything reads it as a path.

        Args:
            queryset: The queryset.
            names: The names given.
            method_name: The method, named in the error.

        Raises:
            QueryError: A name isn't a string.
            FieldError: A name has an empty part - before, between or after ``__``.
        """
        for name in names:
            if not isinstance(name, str):
                raise QueryError(f"{queryset.model.__name__}.objects.{method_name}() takes field names, got {name!r}")
            if "" in name.split("__"):
                raise FieldError(
                    f"{queryset.model.__name__}.objects.{method_name}({name!r}): an empty name between or around '__'"
                )

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
        meta = queryset.model._meta
        fields_map = meta.fields_map
        annotations = queryset._annotations
        for name in names:
            # A name that isn't a string fails on its first use below - told apart only then, so a
            # string name pays for no check.
            try:
                if name in annotations:
                    if isinstance(annotations[name], Lateral):
                        raise FieldError(
                            f"{queryset.model.__name__}.objects.{method_name}({name!r}): a Lateral is read through "
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
            if (
                first_name != PRIMARY_KEY_ALIAS
                and first_name not in fields_map
                and first_name not in annotations
                and first_name not in meta.generic_foreign_key_fields
            ):
                model_name = queryset.model.__name__
                raise FieldError(
                    f"{model_name}.objects.{method_name}({name!r}): {model_name} has no field {first_name!r}"
                )

    @staticmethod
    def takes_pending_filters(queryset: QuerySet[Any, Any], kwargs: dict[str, Any]) -> bool:
        """Whether filter kwargs can be kept unbuilt - their conditions are plain ``Q`` objects, not
        ones built and checked over several key fields (a composite primary key, ``relation__pk``,
        a many-to-many relation) right when ``.filter()`` is called, and their values describe
        themselves for a plan (``is_built_at_call()``).

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
        for key, value in kwargs.items():
            if FilterArguments.is_built_at_call(many_to_many_fields, key, value):
                return False
        return True

    @staticmethod
    def is_built_at_call(many_to_many_fields: Container[str], key: str, value: Any) -> bool:
        """Whether a filter is built and checked right when ``.filter()`` is called rather than kept
        unbuilt: a many-to-many relation or a relation's own key compared through its key fields
        (``__pk``, ``__pk__in``), or a SQL term built by hand - it describes itself by its rendered
        text alone, so a plan can't bind its values anew.

        Args:
            many_to_many_fields: The model's many-to-many fields.
            key: The filter key.
            value: The filter value.

        Returns:
            True when it is built at the call.
        """
        return FilterArguments.is_relation_key_filter(many_to_many_fields, key) or (
            isinstance(value, Term) and not isinstance(value, Plannable)
        )

    @staticmethod
    def is_relation_key_filter(many_to_many_fields: Container[str], key: str) -> bool:
        """Whether the relation key requires building its filter at the call."""
        return key in many_to_many_fields or key.endswith(("__pk", "__pk__in"))

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
        for condition in conditions:
            if isinstance(condition, Exists):
                continue
            if not isinstance(condition, Q):
                return False
            if condition.names_generic_field(queryset.model):
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
        return bool(filters) and FilterArguments.takes_pending_filters(queryset, filters)

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

    @staticmethod
    def describe_get_call(queryset: QuerySet[Any, Any], kwargs: dict[str, Any]) -> tuple[Any, ...] | None:
        """The call ``get(**kwargs)`` of a queryset nothing else changed is in the call signature it
        starts - its filter keys, the types of their values, the row lock and the rows seen, which
        the queryset's other calls don't show. Its values are plain ones, each bound as given.

        Args:
            queryset: The queryset.
            kwargs: The filters.

        Returns:
            The call; None when the queryset was changed otherwise, a filter is built and checked
            right away, or a value is no plain one (a list, a None, a boolean, a query or an
            expression) - ``get()`` then filters it as usual.
        """
        meta = queryset.model._meta
        primary_key_attribute = meta.primary_key_attribute
        # MetaInfo.has_composite_primary_key, read without the property's call: its filters are built
        # over the key fields right away.
        if type(primary_key_attribute) is tuple and primary_key_attribute:
            return None
        call_signature = queryset._call_signature
        lock_structure = None
        visibility_structure = None
        if call_signature is None or len(call_signature) != 1:
            # Not the manager's unchanged queryset (or its all()) - another call clones the queryset with
            # no signature or extends it; only a row lock and the rows seen may be set.
            options = queryset._options
            if options is not QueryOptions.DEFAULT:
                # A queryset whose only option is select_for_update() runs on the plan of its lock.
                if not options.is_default(FilterArguments.non_lock_option_names):
                    return None
                lock_structure = tuple([getattr(options, name) for name in SELECT_FOR_UPDATE_OPTION_NAMES])
            if not (
                not queryset._q_object_list
                and not queryset._pending_filter_calls
                and not queryset._annotations
                and not queryset._select_related
                and not queryset._prefetch_map
                and not queryset._prefetch_queries
                and not queryset._orderings
                and queryset._limit is None
                and not queryset._offset
                and not queryset._distinct
                and not queryset._is_none
                and queryset._selection is None
                and queryset._combination is None
                and meta._inited
            ):
                return None
            visibility = queryset._visibility
            # The default scope's structure is in the key; the rows seen through relations are not.
            if visibility is not RowVisibility.DEFAULT:
                visibility_structure = (visibility.all_tenants, visibility.include_deleted, visibility.only_deleted)
        many_to_many_fields = meta.many_to_many_fields
        for key, value in kwargs.items():
            value_type = type(value)
            if (
                FilterArguments.is_relation_key_filter(many_to_many_fields, key)
                or key.endswith(CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES)
                or (
                    value_type not in PLAIN_VALUE_TYPES
                    and (
                        value_type in CALL_SIGNATURE_DESCRIBED_VALUE_TYPES
                        or isinstance(value, (Plannable, Term, QuerySpecification, Subquery))
                    )
                )
            ):
                return None
        return ("get", tuple(kwargs), tuple(map(type, kwargs.values())), lock_structure, visibility_structure)
