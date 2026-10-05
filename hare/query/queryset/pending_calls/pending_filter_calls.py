from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.enums import Lookup
from hare.query.expressions import Q
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.key_columns import KeyColumns

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.query_specification import QuerySpecification


class PendingFilterCalls:
    """The filter calls a queryset made by simple calls alone keeps unbuilt - built into conditions on
    the first read of the conditions, one generation per call, so the conditions of one call over a
    to-many relation share a JOIN."""

    @staticmethod
    def build_pending_filter_calls(specification: QuerySpecification[Any]) -> None:
        """Builds the filter calls kept unbuilt into conditions, in call order.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        pending_filter_calls = specification._pending_filter_calls
        specification._pending_filter_calls = ()
        # A copy of a query shares its conditions' list - the built ones go into a list of its own.
        specification._q_object_list = list(specification._q_object_list)
        for negate, generation, conditions, kwargs in pending_filter_calls:
            # The Q conditions come stamped with the call's generation already, before the kwargs' -
            # the order QuerySet._append_filters() adds them in.
            PendingFilterCalls.add_filter_conditions(
                specification,
                negate,
                [*conditions, *PendingFilterCalls.get_filter_kwarg_conditions(specification, kwargs, generation)],
                generation,
            )

    @staticmethod
    def get_filter_kwarg_conditions(
        specification: QuerySpecification[Any], kwargs: dict[str, Any], generation: int
    ) -> list[Q]:
        """The conditions of the kwargs of one ``.filter()``/``.exclude()`` call.

        Args:
            specification: The query specification - a queryset or a query made from one.
            kwargs: The filter kwargs.
            generation: The filter-call generation of the call.

        Returns:
            One condition per kwarg.
        """
        # A kwarg's Q is built right here and shared with nothing - stamped in place, not copied.
        conditions = []
        for key, value in kwargs.items():
            condition = PendingFilterCalls.build_filter_q(
                specification, key, FilterValues.get_list_lookup_value(key, value)
            )._stamp_filter_call_generation(generation)
            # Made again for each copy of the query - its value is the call's (PlanOrigins).
            condition._plan_origin = kwargs
            conditions.append(condition)
        return conditions

    @staticmethod
    def add_filter_conditions(
        specification: QuerySpecification[Any], negate: bool, conditions: list[Q], generation: int
    ) -> None:
        """Adds the conditions of one ``.filter()``/``.exclude()`` call to the query's conditions.

        Args:
            specification: The query specification - a queryset or a query made from one.
            negate: Whether the call is ``exclude()``.
            conditions: Its conditions.
            generation: The filter-call generation of the call.
        """
        q_objects = specification._q_objects
        if not negate:
            q_objects.extend(conditions)
        elif len(conditions) == 1:
            q_objects.append(~conditions[0])
        elif conditions:
            # Like Django, one exclude() call drops the rows matching ALL of its conditions:
            # NOT (a AND b), not NOT a AND NOT b.
            q_objects.append(~Q(*conditions)._with_filter_call_generation(generation))

    @staticmethod
    def build_filter_q(specification: QuerySpecification[Any], key: str, value: Any) -> Q:
        """Builds the ``Q`` for one ``.filter()``/``.get()`` kwarg - a key over several fields
        (a composite primary key, ``relation__pk`` of a relation to one, a many-to-many relation
        to one) as comparisons of its fields, so each binds as a plain field value.

        Args:
            specification: The query specification - a queryset or a query made from one.
            key: The filter kwarg.
            value: Its value.

        Returns:
            The ``Q``.
        """
        primary_key_q = KeyColumns.get_primary_key_q(specification.model, key, value)
        if primary_key_q is not None:
            return primary_key_q
        for suffix, lookup, negated in (
            ("__pk__not_in", Lookup.IN, True),
            ("__pk__not", Lookup.EXACT, True),
            ("__pk__in", Lookup.IN, False),
            ("__pk", Lookup.EXACT, False),
        ):
            if not key.endswith(suffix):
                continue
            relation_key = key[: -len(suffix)]
            related_model = getattr(specification.model._meta.fields_map.get(relation_key), "related_model", None)
            if related_model is None or not isinstance(
                target_primary_key_attribute := related_model._meta.primary_key_attribute, tuple
            ):
                break
            field_names = tuple(f"{relation_key}__{name}" for name in target_primary_key_attribute)
            comparison_q = KeyColumns.get_comparison_q(
                key,
                field_names,
                target_primary_key_attribute,
                lookup,
                value,
                accepts_instances=False,
                reads_outer_references=True,
            )
            return ~comparison_q if negated else comparison_q
        field_object = specification.model._meta.fields_map.get(key)
        if isinstance(field_object, ManyToManyFieldInstance) and value is not None:
            target_primary_key_attribute = field_object.related_model._meta.primary_key_attribute
            if isinstance(target_primary_key_attribute, tuple):
                field_names = tuple(f"{key}__{pk_name}" for pk_name in target_primary_key_attribute)
                return KeyColumns.get_comparison_q(
                    key, field_names, target_primary_key_attribute, Lookup.EXACT, value, reads_outer_references=True
                )
        return Q(**{key: value})
