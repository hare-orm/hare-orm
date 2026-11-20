from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import FieldError
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.sql.terms.field import Field

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.rows.native.hydration_layout import HydrationEntry
    from hare.query.statements.select.model_rows_query import ModelRowsQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


class InstanceHydration:
    """How the rows of a query become model instances: the positional decode plan of the selected
    columns, the reader of the rows, what the loaded instances get prefetched, and the refusal of an
    annotation that would overwrite a field or a property of an instance."""

    #: model -> (its basequery_all_fields, the column-name key of that query's SELECT list) - see
    #: get_all_fields_selects_key().
    ALL_FIELDS_SELECTS_KEY_CACHE: ClassVar[ModelCache[tuple[QueryBuilder, tuple[str | None, ...]]]] = ModelCache()

    @staticmethod
    def check_no_annotation_field_collision(query: QuerySpecification[Any]) -> None:
        """Refuses an annotation named like a field or a property of the model - it would overwrite it on the
        instance.

        Args:
            query: The query.

        Raises:
            FieldError: An annotation collides.
        """
        if not query._annotations:
            return
        # An annotation named like a field, or like a property of the model (`pk`), would overwrite
        # it on the hydrated instance. An .alias() is never selected and can't collide.
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        reserved_names = InstanceHydration.get_reserved_attribute_names(model_binding)
        alias_keys = query._alias_keys
        colliding_keys = [key for key in query._annotations if key in reserved_names and key not in alias_keys]
        if colliding_keys:
            raise FieldError(
                f"annotate() key(s) {sorted(colliding_keys)} collide with existing field(s) or "
                f"reserved attribute(s) on model {query.model.__name__} - fetching full model instances "
                "would silently corrupt hydration. Use a different annotation name, or "
                ".values()/.values_list()."
            )

    @staticmethod
    def get_colliding_attribute_names(
        binding: ModelBinding, names: Iterable[str], *, class_attributes: bool = True
    ) -> list[str]:
        """The requested attributes that would replace fields, model state, methods or relation caches.

        Type-only persistence state and dynamic relation caches come from the model's reserved
        names. Class attributes are checked directly, preserving declarations on custom subclasses.
        Unrelated user attributes starting with an underscore remain available. Raw extra
        columns may replace user-declared class defaults, but not callable methods or properties.

        Args:
            binding: The binding of the class whose instances receive the values.
            names: The proposed attribute names.
            class_attributes: Protect every existing class attribute; False permits ordinary
                user class defaults while retaining model state, methods and property checks.

        Returns:
            The conflicting names in sorted order.
        """
        model = binding._model
        reserved_names = InstanceHydration.get_reserved_attribute_names(binding)
        return sorted(
            name
            for name in names
            if name in reserved_names
            or (hasattr(model, name) if class_attributes else callable(getattr(model, name, None)))
        )

    @staticmethod
    @ModelCache.fact()
    def get_reserved_attribute_names(binding: ModelBinding) -> frozenset[str]:
        """The model's fields, persistence state, relation caches, properties and base model API.

        Args:
            binding: The binding supplying the model's fields and relation names.

        Returns:
            The names.
        """
        model = binding._model
        from hare.models.constants import INSTANCE_STATE_ATTRIBUTE_NAMES
        from hare.models.model import Model

        property_names = (
            name
            for klass in model.__mro__
            for name, attribute in vars(klass).items()
            if isinstance(attribute, property)
        )
        return frozenset(
            [
                *binding.fields_map,
                *INSTANCE_STATE_ATTRIBUTE_NAMES,
                *vars(Model),
                *(f"_{name}" for name in binding.fetch_fields),
                *property_names,
            ]
        )

    @staticmethod
    def get_model_rows_reader(
        query: QuerySpecification[Any],
        connection: DatabaseClient | None = None,
        *,
        reader_class: type[ModelRows] = ModelRows,
    ) -> ModelRows:
        """One reader factory for compiled plans, uncacheable statements and streaming.

        With no explicit connection, reuse the statement plan's reader when one is available.
        Otherwise build the reader from the query's layout and the selected connection's dialect.
        The reader holds no connection.
        """
        if connection is None:
            connection = query._connection
            plan = query._statement_plan  # type: ignore[attr-defined]
            if plan is not None:
                reader: ModelRows = plan.result_reading
                return reader
        built_query: ModelRowsQuery[Any] = query  # type: ignore[assignment]
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        return reader_class(
            model_binding,
            connection.dialect.types,  # type: ignore[union-attr]
            select_related_buckets=built_query._select_related_positions,
            decode_plan=built_query._decode_plan,
            decode_plan_is_partial=built_query._decode_plan_is_partial,
            decode_plan_key=built_query._decode_plan_key,
            # An .alias() key is never selected - there's no column to read it from.
            annotations=(
                tuple(name for name in query._annotations if name not in query._alias_keys)
                if query._annotations
                else ()
            ),
            annotation_fields=QueryAnnotations.get_annotation_fields(built_query),
            annotation_columns=built_query._result_column_aliases,
            source_column_names=built_query._source_column_names,
        )

    @staticmethod
    def build_decode_plan(
        query: ModelRowsQuery[Any], base_selects: tuple[Term, ...]
    ) -> tuple[HydrationEntry, ...] | None:
        """The positional decode plan of the base model's selected columns (``base_selects``, taken
        before annotations and joins are added). None when rows aren't read by position.

        Args:
            query: The model rows query.
            base_selects: The base model's selected columns.
        """
        if len(query._select_related_positions) != 1 or not query.features.supports_positional_rows:
            return None
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        entry_by_column = model_binding.get_hydration_layout(query._connection).entry_by_column
        plan: list[HydrationEntry] = []
        for term in base_selects:
            if not isinstance(term, Field):
                return None
            entry = entry_by_column.get(term.name)
            if entry is None:
                return None
            plan.append(entry)
        return tuple(plan)

    @staticmethod
    def get_selects_key(terms: Iterable[Term]) -> tuple[str | None, ...]:
        """Use physical column names and retain the positions of other terms."""
        return tuple(term.name if isinstance(term, Field) else None for term in terms)

    @staticmethod
    def get_all_fields_selects_key(binding: ModelBinding) -> tuple[str | None, ...]:
        """The column names of this binding's every-column SELECT list - part of the decode plans' key and
        of the plan key. Kept per model beside the query it was read from.

        Args:
            binding: The structural source of the SELECT columns.

        Returns:
            One name per selected term, None for a term that isn't a plain column.
        """
        basequery_all_fields = binding.basequery_all_fields
        cached = InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE.get(binding)
        if cached is not None and cached[0] is basequery_all_fields:
            return cached[1]
        selects_key = InstanceHydration.get_selects_key(basequery_all_fields._selects)
        InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE[binding] = (basequery_all_fields, selects_key)
        return selects_key
