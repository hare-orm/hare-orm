from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import FieldError
from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.sql.terms.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
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
    def check_no_annotation_field_collision(query: ModelRowsQuery[Any]) -> None:
        if not query._annotations:
            return
        # An annotation named like a field, or like a property of the model (`pk`), would overwrite
        # it on the hydrated instance. An .alias() is never selected and can't collide.
        reserved_names = InstanceHydration.get_reserved_attribute_names(query.model)
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
    @ModelCache.fact()
    def get_reserved_attribute_names(model: type[Model]) -> frozenset[str]:
        """The names an instance's attributes take - its fields and the properties defined on the
        model class and its bases (``pk``) - a fact of the model.

        Args:
            model: The model class.

        Returns:
            The names.
        """
        property_names = (
            name
            for klass in model.__mro__
            for name, attribute in vars(klass).items()
            if isinstance(attribute, property)
        )
        return frozenset([*model._meta.fields_map, *property_names])

    @staticmethod
    def get_model_rows_reader(query: ModelRowsQuery[Any]) -> ModelRows:
        """How this query's rows are read into instances - shared by ``_execute()`` and
        ``stream()``.

        Args:
            query: The model rows query.

        Returns:
            The reader.
        """
        return ModelRows(
            query.model,
            query._connection,
            select_related_buckets=query._select_related_positions,
            decode_plan=query._decode_plan,
            decode_plan_is_partial=query._decode_plan_is_partial,
            # An .alias() key is never selected - there's no column to read it from.
            annotations=list(query._annotations.keys() - query._alias_keys),
            annotation_fields=QueryAnnotations.get_annotation_fields(query),
        )

    @staticmethod
    def get_prefetch_request(query: ModelRowsQuery[Any]) -> PrefetchRequest | None:
        """What the instances this queryset loads get prefetched, with the settings of this
        queryset the prefetch queries take over - None when nothing is prefetched.

        Args:
            query: The model rows query.

        Returns:
            The request, or None.
        """
        if not (query._prefetch_map or query._prefetch_queries):
            return None
        return PrefetchRequest(
            query._prefetch_map,
            query._prefetch_queries,
            visibility=query._visibility,
            select_for_update=query._select_for_update,
            select_for_update_nowait=query._select_for_update_nowait,
            select_for_update_skip_locked=query._select_for_update_skip_locked,
            select_for_update_strength=query._select_for_update_strength,
            db_explicitly_chosen=query._connection_explicitly_chosen,
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
        entry_by_column = query.model._meta.get_hydration_layout(query._connection).entry_by_column
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
    def get_all_fields_selects_key(model: type[Model]) -> tuple[str | None, ...]:
        """The column names of ``model``'s every-column SELECT list - part of the decode plans' key and
        of the plan key. Kept per model beside the query it was read from.

        Args:
            model: The model.

        Returns:
            One name per selected term, None for a term that isn't a plain column.
        """
        basequery_all_fields = model._meta.basequery_all_fields
        cached = InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE.get(model)
        if cached is not None and cached[0] is basequery_all_fields:
            return cached[1]
        selects_key = tuple(term.name if isinstance(term, Field) else None for term in basequery_all_fields._selects)
        InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE[model] = (basequery_all_fields, selects_key)
        return selects_key
