from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.caching.cache import Cache
from hare.core.constants import CACHE_MISS
from hare.exceptions import FieldError, QueryError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.expressions import Ordering
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.constants import PLAIN_ORDERINGS_CACHE_SIZE
from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitable
from hare.sql import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


SameQuerySet = TypeVar("SameQuerySet", bound="QuerySet[Any, Any]")


class OrderingArguments:
    """What order_by() and the keyset methods are given: the orderings parsed into names and
    directions, the keyset boundaries checked against the ordering, and the primary key ordering a
    slice of an unordered queryset gets."""

    #: Per model, the field names an ``order_by()`` was given as text -> its parsed orderings on a
    #: queryset without annotations; None where a name reads a path ordered by an alias of its
    #: own. A name crosses relations into other models, so a change of any model forgets every
    #: entry.
    plain_orderings: ClassVar[Cache[Any]] = Cache(
        PLAIN_ORDERINGS_CACHE_SIZE, holds_sql=False, depends_on_other_models=True
    )

    @staticmethod
    def get_plain_orderings(
        queryset: QuerySet[Any, Any], orderings: tuple[str | Ordering, ...]
    ) -> tuple[tuple[str, Order], ...] | None:
        """The parsed orderings of field names given as text on a queryset without annotations -
        parsed once per model and call.

        Args:
            queryset: The queryset, with no annotations.
            orderings: What ``order_by()`` was given.

        Returns:
            The orderings ``parse_orderings()`` gives; None where an item is an ``Ordering`` or
            a name reads a path ordered by an alias of its own.
        """
        for ordering in orderings:
            if type(ordering) is not str:
                return None
        model = queryset.model
        parsed = OrderingArguments.plain_orderings.get_for_model(model, (orderings,), CACHE_MISS)
        if parsed is CACHE_MISS:
            names = [queryset._get_ordering_string(ordering)[0] for ordering in orderings]
            if AnnotationArguments.get_path_annotations(queryset, names):
                parsed = None
            else:
                parsed = tuple(OrderingArguments.parse_orderings(queryset, orderings))
            OrderingArguments.plain_orderings[(model, orderings)] = parsed
        return parsed

    @staticmethod
    def parse_orderings(
        queryset: QuerySet[Any, Any],
        orderings: tuple[str | Ordering, ...],
        reverse: bool = False,
        nulls_last_by_default: bool = False,
    ) -> list[tuple[str, Order]]:
        """
        Convert ordering from strings/``Ordering`` objects to standard items for queryset.

        Args:
            queryset: The queryset.
            orderings: What columns/order to order by
            reverse: Whether reverse order
            nulls_last_by_default: Whether an item with no explicit NULL placement, ordering a
                column that can hold NULLs, gets ``NULLS LAST`` - so a NULL never wins
                ``latest()``/``earliest()``. A column that cannot be NULL keeps the plain
                direction (an explicit ``NULLS LAST`` would only cost it index-ordered scans).

        Returns:
            Standard ordering for QuerySet.
        """
        new_ordering = []
        for ordering in orderings:
            field_name, order_type = queryset._get_ordering_string(ordering, reverse=reverse)
            # "pk" orders by every primary key field, a forward relation by its own key column(s).
            if field_name in queryset._annotations or field_name.partition("__")[0] in queryset._annotations:
                ordering_info = AnnotationArguments.get_annotation_description(
                    queryset,
                    "ordering",
                    field_name,
                    lambda: LookupInfoBuilder.get_ordering_info(
                        queryset.model, field_name, annotations=queryset._annotations
                    ),
                )
            else:
                ordering_info = queryset.model._meta.get_ordering_info(field_name)
            for ordering_field_name in ordering_info.paths:
                ordering_type = order_type
                if (
                    nulls_last_by_default
                    and ordering_type.nulls_first is None
                    and queryset._get_field_may_be_null(ordering_field_name)
                ):
                    ordering_type = Order.build(ordering_type.is_ascending, nulls_first=False)
                new_ordering.append((ordering_field_name, ordering_type))
        return new_ordering

    @staticmethod
    def forbid_reordering_after_cursor(queryset: QuerySet[Any, Any], method_name: str) -> None:
        """Raises if ``.after_cursor()``/``.before_cursor()`` was already called - the cursor values
        are bound to the ordering active at that point.

        Args:
            queryset: The queryset.
            method_name: The method replacing or reversing the ordering.

        Raises:
            ValueError: A cursor is already set.
        """
        if queryset._cursor_values or queryset._before_cursor_values:
            cursor_method_name = "before_cursor" if queryset._reverse_result_order else "after_cursor"
            raise QueryError(
                f".{method_name}() cannot be called after .{cursor_method_name}() - its cursor values are "
                f"bound to the ordering that was active when .{cursor_method_name}() was called."
            )

    @staticmethod
    def check_cursor_values(queryset: QuerySet[Any, Any], method_name: str, values: tuple[Any, ...]) -> None:
        """Validates keyset boundary values against the current ordering.

        Args:
            queryset: The queryset.
            method_name: The calling method, for the error message.
            values: The boundary values.

        Raises:
            ValueError: If there is no ``.order_by()``, or the value count doesn't match it.
            FieldError: If an ordering field is an annotation.
        """
        if not queryset._orderings:
            raise QueryError(f".{method_name}() requires .order_by() to be called first")
        if len(values) != len(queryset._orderings):
            raise QueryError(
                f".{method_name}() expects {len(queryset._orderings)} value(s) to match .order_by(), got {len(values)}"
            )
        for field_name, __ in queryset._orderings:
            if field_name in queryset._annotations:
                raise FieldError(f".{method_name}() does not support ordering by an annotation, got '{field_name}'")

    @staticmethod
    def get_cursor_value(obj: Model, field_name: str) -> Any:
        """Reads one ordering field's value off ``obj``, following ``__`` relation hops.

        Args:
            obj: The row to read from.
            field_name: A plain or ``related__field`` ordering field name.

        Returns:
            The value, or ``None`` when a relation along the path is NULL.

        Raises:
            ValueError: If a relation along the path isn't loaded.
            FieldError: If the path crosses a to-many relation.
        """
        from hare.models import Model

        current: Any = obj
        *relation_names, last_name = field_name.split("__")
        for relation_name in relation_names:
            relation_field = type(current)._meta.fields_map.get(relation_name)
            if isinstance(relation_field, (BackwardForeignKeyRelation, ManyToManyFieldInstance)) and not isinstance(
                relation_field, BackwardOneToOneRelation
            ):
                raise FieldError(
                    f".cursor_values() can't read '{field_name}' - '{relation_name}' is a to-many relation"
                )
            related = getattr(current, relation_name)
            if related is None or related is NoneAwaitable:
                return None
            if not isinstance(related, Model):
                raise QueryError(
                    f".cursor_values() can't read '{field_name}' - relation '{relation_name}' isn't loaded, "
                    "use select_related()/prefetch_related()"
                )
            current = related
        return getattr(current, last_name)

    @staticmethod
    def with_primary_key_ordering_if_unordered_slice(queryset: SameQuerySet) -> SameQuerySet:
        """This queryset, ordered by the primary key when it is an unordered slice - the rows
        ``first()`` reads, so every method that picks the rows of the slice by primary key
        picks the same ones.

        Args:
            queryset: The queryset.

        Returns:
            A clone ordered by the primary key, or this queryset itself.
        """
        if (
            (queryset._limit is not None or queryset._offset)
            and not queryset._apply_default_ordering(queryset._orderings, queryset._annotations)
            and not queryset._distinct_on
        ):
            return queryset._with_orderings(*queryset.model._meta.primary_key_attribute_names)
        return queryset
